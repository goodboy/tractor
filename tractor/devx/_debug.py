# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public License
# as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.

"""
Multi-core debugging for da peeps!

"""
from __future__ import annotations
import bdb
from contextlib import (
    asynccontextmanager as acm,
    contextmanager as cm,
    nullcontext,
    _GeneratorContextManager,
    _AsyncGeneratorContextManager,
)
from functools import (
    partial,
    cached_property,
)
import inspect
import os
import signal
import sys
import textwrap
import threading
import traceback
from typing import (
    Any,
    Callable,
    AsyncIterator,
    AsyncGenerator,
    TypeAlias,
    TYPE_CHECKING,
)
from types import (
    FunctionType,
    FrameType,
    ModuleType,
    TracebackType,
    CodeType,
)

from msgspec import Struct
import pdbp
import sniffio
import trio
from trio import CancelScope
from trio.lowlevel import (
    current_task,
    Task,
)
from trio import (
    TaskStatus,
)
import tractor
from tractor.log import get_logger
from tractor._state import (
    current_actor,
    is_root_process,
    debug_mode,
    current_ipc_ctx,
)
# from .pformat import (
#     pformat_caller_frame,
#     pformat_cs,
# )

if TYPE_CHECKING:
    from tractor._ipc import Channel
    from tractor._context import Context
    from tractor._runtime import (
        Actor,
    )
    from tractor.msg import (
        _codec,
    )

log = get_logger(__name__)


def hide_runtime_frames() -> dict[FunctionType, CodeType]:
    '''
    Hide call-stack frames for various std-lib and `trio`-API primitives
    such that the tracebacks presented from our runtime are as minimized
    as possible, particularly from inside a `PdbREPL`.

    '''
    # XXX HACKZONE XXX
    #  hide exit stack frames on nurseries and cancel-scopes!
    # |_ so avoid seeing it when the `pdbp` REPL is first engaged from
    #    inside a `trio.open_nursery()` scope (with no line after it
    #    in before the block end??).
    #
    # TODO: FINALLY got this workin originally with
    #  `@pdbp.hideframe` around the `wrapper()` def embedded inside
    #  `_ki_protection_decoratior()`.. which is in the module:
    #  /home/goodboy/.virtualenvs/tractor311/lib/python3.11/site-packages/trio/_core/_ki.py
    #
    # -[ ] make an issue and patch for `trio` core? maybe linked
    #    to the long outstanding `pdb` one below?
    #   |_ it's funny that there's frame hiding throughout `._run.py`
    #      but not where it matters on the below exit funcs..
    #
    # -[ ] provide a patchset for the lonstanding
    #   |_ https://github.com/python-trio/trio/issues/1155
    #
    # -[ ] make a linked issue to ^ and propose allowing all the
    #     `._core._run` code to have their `__tracebackhide__` value
    #     configurable by a `RunVar` to allow getting scheduler frames
    #     if desired through configuration?
    #
    # -[ ] maybe dig into the core `pdb` issue why the extra frame is shown
    #      at all?
    #
    funcs: list[FunctionType] = [
        trio._core._run.NurseryManager.__aexit__,
        trio._core._run.CancelScope.__exit__,
         _GeneratorContextManager.__exit__,
         _AsyncGeneratorContextManager.__aexit__,
         _AsyncGeneratorContextManager.__aenter__,
         trio.Event.wait,
    ]
    func_list_str: str = textwrap.indent(
        "\n".join(f.__qualname__ for f in funcs),
        prefix=' |_ ',
    )
    log.devx(
        'Hiding the following runtime frames by default:\n'
        f'{func_list_str}\n'
    )

    codes: dict[FunctionType, CodeType] = {}
    for ref in funcs:
        # stash a pre-modified version of each ref's code-obj
        # so it can be reverted later if needed.
        codes[ref] = ref.__code__
        pdbp.hideframe(ref)
    #
    # pdbp.hideframe(trio._core._run.NurseryManager.__aexit__)
    # pdbp.hideframe(trio._core._run.CancelScope.__exit__)
    # pdbp.hideframe(_GeneratorContextManager.__exit__)
    # pdbp.hideframe(_AsyncGeneratorContextManager.__aexit__)
    # pdbp.hideframe(_AsyncGeneratorContextManager.__aenter__)
    # pdbp.hideframe(trio.Event.wait)
    return codes


class LockStatus(
    Struct,
    tag=True,
    tag_field='msg_type',
):
    subactor_uid: tuple[str, str]
    cid: str
    locked: bool


class LockRelease(
    Struct,
    tag=True,
    tag_field='msg_type',
):
    subactor_uid: tuple[str, str]
    cid: str


__pld_spec__: TypeAlias = LockStatus|LockRelease


class Lock:
    '''
    Actor-tree-global debug lock state, exists only in a root process.

    Mostly to avoid a lot of global declarations for now XD.

    '''
    # XXX local ref to the `Pbp` instance, ONLY set in the
    # actor-process that currently has activated a REPL
    # i.e. it will be `None` (unset) in any other actor-process
    # that does not have this lock acquired in the root proc.
    repl: PdbREPL|None = None

    @staticmethod
    def get_locking_task_cs() -> CancelScope|None:
        if not is_root_process():
            raise RuntimeError(
                '`Lock.locking_task_cs` is invalid in subactors!'
            )

        if ctx := Lock.ctx_in_debug:
            return ctx._scope

        return None

    #     ROOT ONLY
    # ------ - -------
    # the root-actor-ONLY singletons for, 
    #
    # - the uid of the actor who's task is using a REPL
    # - a literal task-lock,
    # - a shielded-cancel-scope around the acquiring task*,
    # - a broadcast event to signal no-actor using a REPL in tree,
    # - a filter list to block subs-by-uid from locking.
    #
    # * in case it needs to be manually cancelled in root due to
    #   a stale lock condition (eg. IPC failure with the locking
    #   child
    ctx_in_debug: Context|None = None
    req_handler_finished: trio.Event|None = None

    _debug_lock: trio.StrictFIFOLock = trio.StrictFIFOLock()
    _blocked: set[
        tuple[str, str]  # `Actor.uid` for per actor
        |str  # Context.cid for per task
    ] = set()

    @classmethod
    def repr(cls) -> str:

        # both root and subs
        fields: str = (
            f'repl: {cls.repl}\n'
        )

        if is_root_process():
            lock_stats: trio.LockStatistics = cls._debug_lock.statistics()
            fields += (
                f'req_handler_finished: {cls.req_handler_finished}\n'

                f'_blocked: {cls._blocked}\n\n'
                f'_debug_lock: {cls._debug_lock}\n'
                f'lock_stats: {lock_stats}\n'

            )

        body: str = textwrap.indent(
            fields,
            prefix=' |_',
        )
        return (
            f'<{cls.__name__}(\n'
            f'{body}'
            ')>\n\n'

            f'{cls.ctx_in_debug}\n'
        )

    @classmethod
    @pdbp.hideframe
    def release(
        cls,
        force: bool = False,
    ):
        message: str = 'TTY lock not held by any child\n'

        if not (is_trio_main := DebugStatus.is_main_trio_thread()):
            task: threading.Thread = threading.current_thread()
        else:
            task: trio.Task = current_task()

        try:
            lock: trio.StrictFIFOLock = cls._debug_lock
            owner: Task = lock.statistics().owner
            if (
                lock.locked()
                and
                owner is task
                # ^-NOTE-^ if not will raise a RTE..
            ):
                if not is_trio_main:
                    trio.from_thread.run_sync(
                        cls._debug_lock.release
                    )
                else:
                    cls._debug_lock.release()
                    message: str = 'TTY lock released for child\n'

        finally:
            # IFF there are no more requesting tasks queued up fire, the
            # "tty-unlocked" event thereby alerting any monitors of the lock that
            # we are now back in the "tty unlocked" state. This is basically
            # and edge triggered signal around an empty queue of sub-actor
            # tasks that may have tried to acquire the lock.
            lock_stats = cls._debug_lock.statistics()
            req_handler_finished: trio.Event|None = Lock.req_handler_finished
            if (
                not lock_stats.owner
                or force
                and req_handler_finished is None
            ):
                message += '-> No more child ctx tasks hold the TTY lock!\n'

            elif req_handler_finished:
                req_stats = req_handler_finished.statistics()
                message += (
                    f'-> A child ctx task still owns the `Lock` ??\n'
                    f'  |_lock_stats: {lock_stats}\n'
                    f'  |_req_stats: {req_stats}\n'
                )

            cls.ctx_in_debug = None

    @classmethod
    @acm
    async def acquire(
        cls,
        ctx: Context,

    ) -> AsyncIterator[trio.StrictFIFOLock]:
        '''
        Acquire a root-actor local FIFO lock which tracks mutex access of
        the process tree's global debugger breakpoint.

        This lock avoids tty clobbering (by preventing multiple processes
        reading from stdstreams) and ensures multi-actor, sequential access
        to the ``pdb`` repl.

        '''
        if not is_root_process():
            raise RuntimeError('Only callable by a root actor task!')

        # subactor_uid: tuple[str, str] = ctx.chan.uid
        we_acquired: bool = False
        log.runtime(
            f'Attempting to acquire TTY lock for sub-actor\n'
            f'{ctx}'
        )
        try:
            pre_msg: str = (
                f'Entering lock checkpoint for sub-actor\n'
                f'{ctx}'
            )
            stats = cls._debug_lock.statistics()
            if owner := stats.owner:
                pre_msg += (
                    f'\n'
                    f'`Lock` already held by local task?\n'
                    f'{owner}\n\n'
                    # f'On behalf of task: {cls.remote_task_in_debug!r}\n'
                    f'On behalf of IPC ctx\n'
                    f'{ctx}'
                )
            log.runtime(pre_msg)

            # NOTE: if the surrounding cancel scope from the
            # `lock_tty_for_child()` caller is cancelled, this line should
            # unblock and NOT leave us in some kind of
            # a "child-locked-TTY-but-child-is-uncontactable-over-IPC"
            # condition.
            await cls._debug_lock.acquire()
            cls.ctx_in_debug = ctx
            we_acquired = True

            log.runtime(
                f'TTY lock acquired for sub-actor\n'
                f'{ctx}'
            )

            # NOTE: critical section: this yield is unshielded!

            # IF we received a cancel during the shielded lock entry of some
            # next-in-queue requesting task, then the resumption here will
            # result in that ``trio.Cancelled`` being raised to our caller
            # (likely from ``lock_tty_for_child()`` below)!  In
            # this case the ``finally:`` below should trigger and the
            # surrounding caller side context should cancel normally
            # relaying back to the caller.

            yield cls._debug_lock

        finally:
            message :str = 'Exiting `Lock.acquire()` on behalf of sub-actor\n'
            if we_acquired:
                message += '-> TTY lock released by child\n'
                cls.release()

            else:
                message += '-> TTY lock never acquired by child??\n'

            log.runtime(
                f'{message}\n'
                f'{ctx}'
            )


@tractor.context
async def lock_tty_for_child(
    ctx: Context,
    subactor_task_uid: tuple[str, int],

) -> LockStatus|LockRelease:
    '''
    Lock the TTY in the root process of an actor tree in a new
    inter-actor-context-task such that the ``pdbp`` debugger console
    can be mutex-allocated to the calling sub-actor for REPL control
    without interference by other processes / threads.

    NOTE: this task must be invoked in the root process of the actor
    tree. It is meant to be invoked as an rpc-task and should be
    highly reliable at releasing the mutex complete!

    '''
    subactor_uid: tuple[str, str] = ctx.chan.uid

    # mark the tty lock as being in use so that the runtime
    # can try to avoid clobbering any connection from a child
    # that's currently relying on it.
    we_finished = Lock.req_handler_finished = trio.Event()
    try:
        if ctx.cid in Lock._blocked:
            raise RuntimeError(
                f'Double lock request!?\n'
                f'The same remote task already has an active request for TTY lock ??\n\n'
                f'subactor uid: {subactor_uid}\n\n'

                'This might be mean that the requesting task '
                'in `request_root_stdio_lock()` may have crashed?\n'
                'Consider that an internal bug exists given the TTY '
                '`Lock`ing IPC dialog..\n'
            )

        root_task_name: str = current_task().name
        if tuple(subactor_uid) in Lock._blocked:
            log.warning(
                f'Subactor is blocked from acquiring debug lock..\n'
                f'subactor_uid: {subactor_uid}\n'
                f'remote task: {subactor_task_uid}\n'
            )
            ctx._enter_debugger_on_cancel: bool = False
            message: str = (
                f'Debug lock blocked for {subactor_uid}\n'
                'Cancelling debug request!\n'
            )
            log.cancel(message)
            await ctx.cancel()
            raise DebugRequestError(message)

        log.devx(
            'Subactor attempting to acquire TTY lock\n'
            f'root task: {root_task_name}\n'
            f'subactor_uid: {subactor_uid}\n'
            f'remote task: {subactor_task_uid}\n'
        )
        DebugStatus.shield_sigint()
        Lock._blocked.add(ctx.cid)

        # NOTE: we use the IPC ctx's cancel scope directly in order to
        # ensure that on any transport failure, or cancellation request
        # from the child we expect
        # `Context._maybe_cancel_and_set_remote_error()` to cancel this
        # scope despite the shielding we apply below.
        debug_lock_cs: CancelScope = ctx._scope

        # TODO: use `.msg._ops.maybe_limit_plds()` here instead so we
        # can merge into a single async with, with the
        # `Lock.acquire()` enter below?
        #
        # enable the locking msgspec
        with apply_debug_pldec():
            async with Lock.acquire(ctx=ctx):
                debug_lock_cs.shield = True

                log.devx(
                    'Subactor acquired debugger request lock!\n'
                    f'root task: {root_task_name}\n'
                    f'subactor_uid: {subactor_uid}\n'
                    f'remote task: {subactor_task_uid}\n\n'

                    'Sending `ctx.started(LockStatus)`..\n'

                )

                # indicate to child that we've locked stdio
                await ctx.started(
                    LockStatus(
                        subactor_uid=subactor_uid,
                        cid=ctx.cid,
                        locked=True,
                    )
                )

                log.devx(
                    f'Actor {subactor_uid} acquired `Lock` via debugger request'
                )

                # wait for unlock pdb by child
                async with ctx.open_stream() as stream:
                    release_msg: LockRelease = await stream.receive()

                    # TODO: security around only releasing if
                    # these match?
                    log.devx(
                        f'TTY lock released requested\n\n'
                        f'{release_msg}\n'
                    )
                    assert release_msg.cid == ctx.cid
                    assert release_msg.subactor_uid == tuple(subactor_uid)

                log.devx(
                    f'Actor {subactor_uid} released TTY lock'
                )

            return LockStatus(
                subactor_uid=subactor_uid,
                cid=ctx.cid,
                locked=False,
            )

    except BaseException as req_err:
        message: str = (
            'Forcing `Lock.release()` for req-ctx since likely an '
            'internal error!\n\n'
            f'{ctx}'
        )
        if isinstance(req_err, trio.Cancelled):
            message = (
                'Cancelled during root TTY-lock dialog?\n'
                +
                message
            )
        else:
            message = (
                'Errored during root TTY-lock dialog?\n'
                +
                message
            )

        log.exception(message)
        Lock.release(force=True)
        raise

    finally:
        Lock._blocked.remove(ctx.cid)

        # wakeup any waiters since the lock was (presumably)
        # released, possibly only temporarily.
        we_finished.set()
        DebugStatus.unshield_sigint()


# TODO: rename to ReplState or somethin?
# DebugRequest, make it a singleton instance?
class DebugStatus:
    '''
    Singleton-state for debugging machinery in a subactor.

    Composes conc primitives for syncing with a root actor to
    acquire the tree-global (TTY) `Lock` such that only ever one
    actor's task can have the REPL active at a given time.

    Methods to shield the process' `SIGINT` handler are used
    whenever a local task is an active REPL.

    '''
    repl: PdbREPL|None = None

    # TODO: yet again this looks like a task outcome where we need
    # to sync to the completion of one task (and get its result)
    # being used everywhere for syncing..
    # -[ ] see if we can get our proto oco task-mngr to work for
    #   this?
    repl_task: Task|None = None
    repl_release: trio.Event|None = None

    req_task: Task|None = None
    req_ctx: Context|None = None
    req_cs: CancelScope|None = None
    req_finished: trio.Event|None = None
    req_err: BaseException|None = None

    lock_status: LockStatus|None = None

    _orig_sigint_handler: Callable|None = None
    _trio_handler: (
        Callable[[int, FrameType|None], Any]
        |int
        | None
    ) = None

    @classmethod
    def repr(cls) -> str:
        fields: str = (
            f'repl: {cls.repl}\n'
            f'repl_task: {cls.repl_task}\n'
            f'repl_release: {cls.repl_release}\n'
            f'req_ctx: {cls.req_ctx}\n'
        )
        body: str = textwrap.indent(
            fields,
            prefix=' |_',
        )
        return (
            f'<{cls.__name__}(\n'
            f'{body}'
            ')>'
        )

    # TODO: how do you get this to work on a non-inited class?
    # __repr__ = classmethod(repr)
    # __str__ = classmethod(repr)

    @classmethod
    def shield_sigint(cls):
        '''
        Shield out SIGINT handling (which by default triggers
        `trio.Task` cancellation) in subactors when a `pdb` REPL
        is active.

        Avoids cancellation of the current actor (task) when the user
        mistakenly sends ctl-c or via a recevied signal (from an
        external request). Explicit runtime cancel requests are
        allowed until the current REPL-session (the blocking call
        `Pdb.interaction()`) exits, normally via the 'continue' or
        'quit' command - at which point the orig SIGINT handler is
        restored via `.unshield_sigint()` below.

        Impl notes:
        -----------
        - we prefer that `trio`'s default handler is always used when
          SIGINT is unshielded (hence disabling the `pdb.Pdb`
          defaults in `mk_pdb()`) such that reliable KBI cancellation
          is always enforced.

        - we always detect whether we're running from a non-main
          thread, in which case schedule the SIGINT shielding override
          to in the main thread as per,

          https://docs.python.org/3/library/signal.html#signals-and-threads

        '''
        #
        # XXX detect whether we're running from a non-main thread
        # in which case schedule the SIGINT shielding override
        # to in the main thread.
        # https://docs.python.org/3/library/signal.html#signals-and-threads
        if not cls.is_main_trio_thread():
            cls._orig_sigint_handler: Callable = trio.from_thread.run_sync(
                signal.signal,
                signal.SIGINT,
                shield_sigint_handler,
            )

        else:
            cls._orig_sigint_handler = signal.signal(
                signal.SIGINT,
                shield_sigint_handler,
            )

    @classmethod
    @pdbp.hideframe  # XXX NOTE XXX see below in `.pause_from_sync()`
    def unshield_sigint(cls):
        '''
        Un-shield SIGINT for REPL-active (su)bactor.

        See details in `.shield_sigint()`.

        '''
        # always restore ``trio``'s sigint handler. see notes below in
        # the pdb factory about the nightmare that is that code swapping
        # out the handler when the repl activates...
        if not cls.is_main_trio_thread():
            trio.from_thread.run_sync(
                signal.signal,
                signal.SIGINT,
                cls._trio_handler,
            )
        else:
            trio_h: Callable = cls._trio_handler
            # XXX should never really happen XXX
            if not trio_h:
                mk_pdb().set_trace()

            signal.signal(
                signal.SIGINT,
                cls._trio_handler,
            )

        cls._orig_sigint_handler = None

    @classmethod
    def is_main_trio_thread(cls) -> bool:
        '''
        Check if we're the "main" thread (as in the first one
        started by cpython) AND that it is ALSO the thread that
        called `trio.run()` and not some thread spawned with
        `trio.to_thread.run_sync()`.

        '''
        is_trio_main = (
            # TODO: since this is private, @oremanj says
            # we should just copy the impl for now..
            (is_main_thread := trio._util.is_main_thread())
            and
            (async_lib := sniffio.current_async_library()) == 'trio'
        )
        if (
            not is_trio_main
            and is_main_thread
        ):
            log.warning(
                f'Current async-lib detected by `sniffio`: {async_lib}\n'
            )
        return is_trio_main
        # XXX apparently unreliable..see ^
        # (
        #     threading.current_thread()
        #     is not threading.main_thread()
        # )

    @classmethod
    def cancel(cls) -> bool:
        if (req_cs := cls.req_cs):
            req_cs.cancel()
            return True

        return False

    @classmethod
    @pdbp.hideframe
    def release(
        cls,
        cancel_req_task: bool = False,
    ):
        repl_release: trio.Event = cls.repl_release
        try:
            # sometimes the task might already be terminated in
            # which case this call will raise an RTE?
            if repl_release is not None:
                repl_release.set()

        finally:
            # if req_ctx := cls.req_ctx:
            #     req_ctx._scope.cancel()
            if cancel_req_task:
                cancelled: bool = cls.cancel()
                if not cancelled:
                    log.warning(
                        'Failed to cancel request task!?\n'
                        f'{cls.repl_task}\n'
                    )

            # actor-local state, irrelevant for non-root.
            cls.repl_task = None
            cls.repl = None

            # restore original sigint handler
            cls.unshield_sigint()


class TractorConfig(pdbp.DefaultConfig):
    '''
    Custom `pdbp` config which tries to use the best tradeoff
    between pretty and minimal.

    '''
    use_pygments: bool = True
    sticky_by_default: bool = False
    enable_hidden_frames: bool = True

    # much thanks @mdmintz for the hot tip!
    # fixes line spacing issue when resizing terminal B)
    truncate_long_lines: bool = False

    # ------ - ------
    # our own custom config vars mostly
    # for syncing with the actor tree's singleton
    # TTY `Lock`.


class PdbREPL(pdbp.Pdb):
    '''
    Add teardown hooks and local state describing any 
    ongoing TTY `Lock` request dialog.

    '''
    # override the pdbp config with our coolio one
    # NOTE: this is only loaded when no `~/.pdbrc` exists
    # so we should prolly pass it into the .__init__() instead?
    # i dunno, see the `DefaultFactory` and `pdb.Pdb` impls.
    DefaultConfig = TractorConfig

    status = DebugStatus

    # NOTE: see details in stdlib's `bdb.py`
    # def user_exception(self, frame, exc_info):
    #     '''
    #     Called when we stop on an exception.
    #     '''
    #     log.warning(
    #         'Exception during REPL sesh\n\n'
    #         f'{frame}\n\n'
    #         f'{exc_info}\n\n'
    #     )

    # NOTE: this actually hooks but i don't see anyway to detect
    # if an error was caught.. this is why currently we just always
    # call `DebugStatus.release` inside `_post_mortem()`.
    # def preloop(self):
    #     print('IN PRELOOP')
    #     super().preloop()

    # TODO: cleaner re-wrapping of all this?
    # -[ ] figure out how to disallow recursive .set_trace() entry
    #     since that'll cause deadlock for us.
    # -[ ] maybe a `@cm` to call `super().<same_meth_name>()`?
    # -[ ] look at hooking into the `pp` hook specially with our
    #     own set of pretty-printers?
    #    * `.pretty_struct.Struct.pformat()`
    #    * `.pformat(MsgType.pld)`
    #    * `.pformat(Error.tb_str)`?
    #    * .. maybe more?
    #
    def set_continue(self):
        try:
            super().set_continue()
        finally:
            DebugStatus.release()

            # NOTE: for subactors the stdio lock is released via the
            # allocated RPC locker task, so for root we have to do it
            # manually.
            if (
                is_root_process()
                and
                Lock._debug_lock.locked()
            ):
                Lock.release()

    def set_quit(self):
        try:
            super().set_quit()
        finally:
            DebugStatus.release()
            if (
                is_root_process()
                and
                Lock._debug_lock.locked()
            ):
                Lock.release()

    # XXX NOTE: we only override this because apparently the stdlib pdb
    # bois likes to touch the SIGINT handler as much as i like to touch
    # my d$%&.
    def _cmdloop(self):
        self.cmdloop()

    @cached_property
    def shname(self) -> str | None:
        '''
        Attempt to return the login shell name with a special check for
        the infamous `xonsh` since it seems to have some issues much
        different from std shells when it comes to flushing the prompt?

        '''
        # SUPER HACKY and only really works if `xonsh` is not used
        # before spawning further sub-shells..
        shpath = os.getenv('SHELL', None)

        if shpath:
            if (
                os.getenv('XONSH_LOGIN', default=False)
                or 'xonsh' in shpath
            ):
                return 'xonsh'

            return os.path.basename(shpath)

        return None


# TODO: prolly remove this and instead finally get our @context API
# supporting a msg/pld-spec via type annots as per,
# https://github.com/goodboy/tractor/issues/365
@cm
def apply_debug_pldec() -> _codec.MsgCodec:
    '''
    Apply the subactor TTY `Lock`-ing protocol's msgspec temporarily
    (only in the current task).

    '''
    from tractor.msg import (
        _ops as msgops,
    )
    cctx: Context = current_ipc_ctx()
    rx: msgops.PldRx = cctx.pld_rx
    orig_pldec: msgops.MsgDec = rx.pld_dec

    try:
        with msgops.limit_plds(
            spec=__pld_spec__,
        ) as debug_dec:
            assert (
                debug_dec
                is
                rx.pld_dec
            )
            log.runtime(
                'Applied `.devx._debug` pld-spec\n\n'
                f'{debug_dec}\n'
            )
            yield debug_dec

    finally:
        assert (
            rx.pld_dec is orig_pldec
        )
        log.runtime(
            'Reverted to previous pld-spec\n\n'
            f'{orig_pldec}\n'
        )


async def request_root_stdio_lock(
    actor_uid: tuple[str, str],
    task_uid: tuple[str, int],

    shield: bool = False,
    task_status: TaskStatus[CancelScope] = trio.TASK_STATUS_IGNORED,
):
    '''
    Connect to the root actor of this process tree and RPC-invoke
    a task which acquires a std-streams global `Lock`: a actor tree
    global mutex which prevents other subactors from entering
    a `PdbREPL` at the same time as any other.

    The actual `Lock` singleton exists ONLY in the root actor's
    memory and does nothing more then set process-tree global state.
    The actual `PdbREPL` interaction is completely isolated to each
    sub-actor and with the `Lock` merely providing the multi-process
    syncing mechanism to avoid any subactor (or the root itself) from
    entering the REPL at the same time.

    '''

    log.devx(
        'Initing stdio-lock request task with root actor'
    )
    # TODO: likely we can implement this mutex more generally as
    #      a `._sync.Lock`?
    # -[ ] simply add the wrapping needed for the debugger specifics?
    #   - the `__pld_spec__` impl and maybe better APIs for the client
    #   vs. server side state tracking? (`Lock` + `DebugStatus`)
    # -[ ] for eg. `mp` has a multi-proc lock via the manager
    #   - https://docs.python.org/3.8/library/multiprocessing.html#synchronization-primitives
    # -[ ] technically we need a `RLock` since re-acquire should be a noop
    #   - https://docs.python.org/3.8/library/multiprocessing.html#multiprocessing.RLock
    DebugStatus.req_finished = trio.Event()
    DebugStatus.req_task = current_task()
    try:
        from tractor._discovery import get_root
        # NOTE: we need this to ensure that this task exits
        # BEFORE the REPl instance raises an error like
        # `bdb.BdbQuit` directly, OW you get a trio cs stack
        # corruption!
        # Further, the since this task is spawned inside the
        # `Context._scope_nursery: trio.Nursery`, once an RPC
        # task errors that cs is cancel_called and so if we want
        # to debug the TPC task that failed we need to shield
        # against that expected `.cancel()` call and instead
        # expect all of the `PdbREPL`.set_[continue/quit/]()`
        # methods to unblock this task by setting the
        # `.repl_release: # trio.Event`.
        with trio.CancelScope(shield=shield) as req_cs:
            # XXX: was orig for debugging cs stack corruption..
            # log.info(
            #     'Request cancel-scope is:\n\n'
            #     f'{pformat_cs(req_cs, var_name="req_cs")}\n\n'
            # )
            DebugStatus.req_cs = req_cs
            req_ctx: Context|None = None
            try:
                # TODO: merge into single async with ?
                async with get_root() as portal:
                    async with portal.open_context(
                        lock_tty_for_child,
                        subactor_task_uid=task_uid,
                        # NOTE: set it here in the locker request task bc it's
                        # possible for multiple such requests for the lock in any
                        # single sub-actor AND there will be a race between when the
                        # root locking task delivers the `Started(pld=LockStatus)`
                        # and when the REPL is actually entered by the requesting
                        # application task who called
                        # `.pause()`/`.post_mortem()`.
                        #
                        # SO, applying the pld-spec here means it is only applied to
                        # this IPC-ctx request task, NOT any other task(s)
                        # including the one that actually enters the REPL. This
                        # is oc desired bc ow the debugged task will msg-type-error.
                        pld_spec=__pld_spec__,

                    ) as (req_ctx, status):

                        DebugStatus.req_ctx = req_ctx
                        log.devx(
                            'Subactor locked TTY with msg\n\n'
                            f'{status}\n'
                        )

                        # try:
                        assert status.subactor_uid == actor_uid
                        assert status.cid
                        # except AttributeError:
                        #     log.exception('failed pldspec asserts!')
                        #     mk_pdb().set_trace()
                        #     raise

                        # set last rxed lock dialog status.
                        DebugStatus.lock_status = status

                        async with req_ctx.open_stream() as stream:
                            task_status.started(req_ctx)

                            # wait for local task to exit
                            # `PdbREPL.interaction()`, normally via
                            # a `DebugStatus.release()`call,  and
                            # then unblock us here.
                            await DebugStatus.repl_release.wait()
                            await stream.send(
                                LockRelease(
                                    subactor_uid=actor_uid,
                                    cid=status.cid,
                                )
                            )

                            # sync with child-side root locker task
                            # completion
                            status: LockStatus = await req_ctx.result()
                            assert not status.locked
                            DebugStatus.lock_status = status

                    log.devx(
                        'TTY lock was released for subactor with msg\n\n'
                        f'{status}\n\n'
                        f'Exitting {req_ctx.side!r}-side of locking req_ctx\n'
                    )

            except (
                tractor.ContextCancelled,
                trio.Cancelled,
            ):
                log.cancel(
                    'Debug lock request was CANCELLED?\n\n'
                    f'{req_ctx}\n'
                    # f'{pformat_cs(req_cs, var_name="req_cs")}\n\n'
                    # f'{pformat_cs(req_ctx._scope, var_name="req_ctx._scope")}\n\n'
                )
                raise

            except (
                BaseException,
            ) as ctx_err:
                message: str = (
                    'Failed during debug request dialog with root actor?\n\n'
                )

                if req_ctx:
                    message += (
                        f'{req_ctx}\n'
                        f'Cancelling IPC ctx!\n'
                    )
                    await req_ctx.cancel()

                else:
                    message += 'Failed during `Portal.open_context()` ?\n'

                log.exception(message)
                ctx_err.add_note(message)
                raise ctx_err


    except (
        tractor.ContextCancelled,
        trio.Cancelled,
    ):
        log.cancel(
            'Debug lock request CANCELLED?\n'
            f'{req_ctx}\n'
        )
        raise

    except BaseException as req_err:
        # log.error('Failed to request root stdio-lock?')
        DebugStatus.req_err = req_err
        DebugStatus.release()

        # TODO: how to dev a test that ensures we actually drop
        # into THIS internal frame on any internal error in the above
        # code?
        # -[ ] eg. on failed pld_dec assert above we should be able
        #   to REPL pm it.
        # -[ ]FURTHER, after we 'continue', we should be able to
        #   ctl-c out of the currently hanging task! 
        raise DebugRequestError(
            'Failed to lock stdio from subactor IPC ctx!\n\n'

            f'req_ctx: {DebugStatus.req_ctx}\n'
        ) from req_err

    finally:
        log.devx('Exiting debugger TTY lock request func from child')
        # signal request task exit
        DebugStatus.req_finished.set()
        DebugStatus.req_task = None


def mk_pdb() -> PdbREPL:
    '''
    Deliver a new `PdbREPL`: a multi-process safe `pdbp.Pdb`-variant
    using the magic of `tractor`'s SC-safe IPC.

    B)

    Our `pdb.Pdb` subtype accomplishes multi-process safe debugging
    by:

    - mutexing access to the root process' std-streams (& thus parent
      process TTY) via an IPC managed `Lock` singleton per
      actor-process tree.

    - temporarily overriding any subactor's SIGINT handler to shield
      during live REPL sessions in sub-actors such that cancellation
      is never (mistakenly) triggered by a ctrl-c and instead only by
      explicit runtime API requests or after the
      `pdb.Pdb.interaction()` call has returned.

    FURTHER, the `pdbp.Pdb` instance is configured to be `trio`
    "compatible" from a SIGINT handling perspective; we mask out
    the default `pdb` handler and instead apply `trio`s default
    which mostly addresses all issues described in:

     - https://github.com/python-trio/trio/issues/1155

    The instance returned from this factory should always be
    preferred over the default `pdb[p].set_trace()` whenever using
    a `pdb` REPL inside a `trio` based runtime.

    '''
    pdb = PdbREPL()

    # XXX: These are the important flags mentioned in
    # https://github.com/python-trio/trio/issues/1155
    # which resolve the traceback spews to console.
    pdb.allow_kbdint = True
    pdb.nosigint = True
    return pdb


def any_connected_locker_child() -> bool:
    '''
    Predicate to determine if a reported child subactor in debug
    is actually connected.

    Useful to detect stale `Lock` requests after IPC failure.

    '''
    actor: Actor = current_actor()

    if not is_root_process():
        raise RuntimeError('This is a root-actor only API!')

    if (
        (ctx := Lock.ctx_in_debug)
        and
        (uid_in_debug := ctx.chan.uid)
    ):
        chans: list[tractor.Channel] = actor._peers.get(
            tuple(uid_in_debug)
        )
        if chans:
            return any(
                chan.connected()
                for chan in chans
            )

    return False


def shield_sigint_handler(
    signum: int,
    frame: 'frame',  # type: ignore # noqa
    *args,

) -> None:
    '''
    Specialized, debugger-aware SIGINT handler.

    In childred we always ignore/shield for SIGINT to avoid
    deadlocks since cancellation should always be managed by the
    supervising parent actor. The root actor-proces is always
    cancelled on ctrl-c.

    '''
    __tracebackhide__: bool = True
    actor: Actor = current_actor()

    def do_cancel():
        # If we haven't tried to cancel the runtime then do that instead
        # of raising a KBI (which may non-gracefully destroy
        # a ``trio.run()``).
        if not actor._cancel_called:
            actor.cancel_soon()

        # If the runtime is already cancelled it likely means the user
        # hit ctrl-c again because teardown didn't fully take place in
        # which case we do the "hard" raising of a local KBI.
        else:
            raise KeyboardInterrupt

    # only set in the actor actually running the REPL
    repl: PdbREPL|None = DebugStatus.repl

    # TODO: maybe we should flatten out all these cases using
    # a match/case?
    #
    # root actor branch that reports whether or not a child
    # has locked debugger.
    if is_root_process():
        # try to see if the supposed (sub)actor in debug still
        # has an active connection to *this* actor, and if not
        # it's likely they aren't using the TTY lock / debugger
        # and we should propagate SIGINT normally.
        any_connected: bool = any_connected_locker_child()
        # if not any_connected:
        #     return do_cancel()

        problem = (
            f'root {actor.uid} handling SIGINT\n'
            f'any_connected: {any_connected}\n\n'

            f'{Lock.repr()}\n'
        )

        if (
            (ctx := Lock.ctx_in_debug)
            and
            (uid_in_debug := ctx.chan.uid) # "someone" is (ostensibly) using debug `Lock`
        ):
            name_in_debug: str = uid_in_debug[0]
            assert not repl
            # if not repl:  # but it's NOT us, the root actor.
            # sanity: since no repl ref is set, we def shouldn't
            # be the lock owner!
            assert name_in_debug != 'root'

            # IDEAL CASE: child has REPL as expected
            if any_connected:  # there are subactors we can contact
                # XXX: only if there is an existing connection to the
                # (sub-)actor in debug do we ignore SIGINT in this
                # parent! Otherwise we may hang waiting for an actor
                # which has already terminated to unlock.
                #
                # NOTE: don't emit this with `.pdb()` level in
                # root without a higher level.
                log.runtime(
                    f'Ignoring SIGINT while debug REPL in use by child '
                    f'{uid_in_debug}\n'
                )
                problem = None

            else:
                problem += (
                    '\n'
                    f'A `pdb` REPL is SUPPOSEDLY in use by child {uid_in_debug}\n'
                    f'BUT, no child actors are IPC contactable!?!?\n'
                )

        # IDEAL CASE: root has REPL as expected
        else:
            # root actor still has this SIGINT handler active without
            # an actor using the `Lock` (a bug state) ??
            # => so immediately cancel any stale lock cs and revert
            # the handler!
            if not repl:
                # TODO: WHEN should we revert back to ``trio``
                # handler if this one is stale?
                # -[ ] maybe after a counts work of ctl-c mashes?
                # -[ ] use a state var like `stale_handler: bool`?
                problem += (
                    '\n'
                    'No subactor is using a `pdb` REPL according `Lock.ctx_in_debug`?\n'
                    'BUT, the root should be using it, WHY this handler ??\n'
                )
            else:
                log.pdb(
                    'Ignoring SIGINT while pdb REPL in use by root actor..\n'
                )
                problem = None

        # XXX if one is set it means we ARE NOT operating an ideal
        # case where a child subactor or us (the root) has the
        # lock without any other detected problems.
        if problem:

            # detect, report and maybe clear a stale lock request
            # cancel scope.
            lock_cs: trio.CancelScope = Lock.get_locking_task_cs()
            maybe_stale_lock_cs: bool = (
                lock_cs is not None
                and not lock_cs.cancel_called
            )
            if maybe_stale_lock_cs:
                problem += (
                    '\n'
                    'Stale `Lock.ctx_in_debug._scope: CancelScope` detected?\n'
                    f'{Lock.ctx_in_debug}\n\n'

                    '-> Calling ctx._scope.cancel()!\n'
                )
                lock_cs.cancel()

            # TODO: wen do we actually want/need this, see above.
            # DebugStatus.unshield_sigint()
            log.warning(problem)

    # child actor that has locked the debugger
    elif not is_root_process():
        log.debug(
            f'Subactor {actor.uid} handling SIGINT\n\n'
            f'{Lock.repr()}\n'
        )

        rent_chan: Channel = actor._parent_chan
        if (
            rent_chan is None
            or
            not rent_chan.connected()
        ):
            log.warning(
                'This sub-actor thinks it is debugging '
                'but it has no connection to its parent ??\n'
                f'{actor.uid}\n'
                'Allowing SIGINT propagation..'
            )
            DebugStatus.unshield_sigint()
            # do_cancel()

        repl_task: str|None = DebugStatus.repl_task
        req_task: str|None = DebugStatus.req_task
        if (
            repl_task
            and
            repl
        ):
            log.pdb(
                f'Ignoring SIGINT while local task using debug REPL\n'
                f'|_{repl_task}\n'
                f'  |_{repl}\n'
            )
        elif req_task:
            log.pdb(
                f'Ignoring SIGINT while debug request task is open\n'
                f'|_{req_task}\n'
            )
        else:
            msg: str = (
                'SIGINT shield handler still active BUT, \n\n'
            )
            if repl_task is None:
                msg += (
                    '- No local task claims to be in debug?\n'
                )

            if repl is None:
                msg += (
                    '- No local REPL is currently active?\n'
                )

            if req_task is None:
                msg += (
                    '- No debug request task is active?\n'
                )

            log.warning(
                msg
                +
                'Reverting handler to `trio` default!\n'
            )
            DebugStatus.unshield_sigint()

            # XXX ensure that the reverted-to-handler actually is
            # able to rx what should have been **this** KBI ;)
            do_cancel()

        # TODO: how to handle the case of an intermediary-child actor
        # that **is not** marked in debug mode? See oustanding issue:
        # https://github.com/goodboy/tractor/issues/320
        # elif debug_mode():

    # NOTE: currently (at least on ``fancycompleter`` 0.9.2)
    # it looks to be that the last command that was run (eg. ll)
    # will be repeated by default.

    # maybe redraw/print last REPL output to console since
    # we want to alert the user that more input is expect since
    # nothing has been done dur to ignoring sigint.
    if (
        repl  # only when current actor has a REPL engaged
    ):
        # XXX: yah, mega hack, but how else do we catch this madness XD
        if repl.shname == 'xonsh':
            repl.stdout.write(repl.prompt)

        repl.stdout.flush()

        # TODO: make this work like sticky mode where if there is output
        # detected as written to the tty we redraw this part underneath
        # and erase the past draw of this same bit above?
        # repl.sticky = True
        # repl._print_if_sticky()

        # also see these links for an approach from ``ptk``:
        # https://github.com/goodboy/tractor/issues/130#issuecomment-663752040
        # https://github.com/prompt-toolkit/python-prompt-toolkit/blob/c2c6af8a0308f9e5d7c0e28cb8a02963fe0ce07a/prompt_toolkit/patch_stdout.py

    # XXX only for tracing this handler
    log.devx('exiting SIGINT')


_pause_msg: str = 'Attaching to pdb REPL in actor'


class DebugRequestError(RuntimeError):
    '''
    Failed to request stdio lock from root actor!

    '''


async def _pause(

    debug_func: Callable|None,

    # NOTE: must be passed in the `.pause_from_sync()` case!
    repl: PdbREPL|None = None,

    # TODO: allow caller to pause despite task cancellation,
    # exactly the same as wrapping with:
    # with CancelScope(shield=True):
    #     await pause()
    # => the REMAINING ISSUE is that the scope's .__exit__() frame
    # is always show in the debugger on entry.. and there seems to
    # be no way to override it?..
    #
    shield: bool = False,
    hide_tb: bool = False,
    task_status: TaskStatus[trio.Event] = trio.TASK_STATUS_IGNORED,
    **debug_func_kwargs,

) -> None:
    '''
    Inner impl for `pause()` to avoid the `trio.CancelScope.__exit__()`
    stack frame when not shielded (since apparently i can't figure out
    how to hide it using the normal mechanisms..)

    Hopefully we won't need this in the long run.

    '''
    __tracebackhide__: bool = hide_tb
    actor: Actor = current_actor()
    try:
        # TODO: use the `Task` instance instead for `is` checks
        # below!
        task: Task = trio.lowlevel.current_task()
    except RuntimeError as rte:
        if actor.is_infected_aio():
            raise RuntimeError(
                '`tractor.pause[_from_sync]()` not yet supported '
                'for infected `asyncio` mode!'
            ) from rte

    if debug_func is not None:
        debug_func = partial(debug_func)

    repl: PdbREPL = repl or mk_pdb()

    # XXX NOTE XXX set it here to avoid ctl-c from cancelling a debug
    # request from a subactor BEFORE the REPL is entered by that
    # process.
    DebugStatus.shield_sigint()

    # TODO: move this into a `open_debug_request()` @acm?
    # -[ ] prolly makes the most sense to do the request
    #    task spawn as part of an `@acm` api which delivers the
    #    `DebugRequest` instance and ensures encapsing all the
    #    pld-spec and debug-nursery?
    # -[ ] maybe make this a `PdbREPL` method or mod func?
    # -[ ] factor out better, main reason for it is common logic for
    #   both root and sub repl entry
    def _enter_repl_sync(
        debug_func: Callable,
    ) -> None:
        __tracebackhide__: bool = hide_tb

        try:
            # set local actor task to avoid recurrent
            # entries/requests from the same local task (to the root
            # process).
            DebugStatus.repl_task = task
            DebugStatus.repl = repl

            # TODO: do we want to support using this **just** for the
            # locking / common code (prolly to help address #320)?
            if debug_func is None:
                task_status.started(DebugStatus)

            else:
                log.warning(
                    'Entering REPL for task fuck you!\n'
                    f'{task}\n'
                )
                # block here one (at the appropriate frame *up*) where
                # ``breakpoint()`` was awaited and begin handling stdio.
                log.devx(
                    'Entering sync world of the `pdb` REPL for task..\n'
                    f'{repl}\n'
                    f'  |_{task}\n'
                 )

                # invoke the low-level REPL activation routine which itself
                # should call into a `Pdb.set_trace()` of some sort.
                debug_func(
                    repl=repl,
                    hide_tb=hide_tb,
                    **debug_func_kwargs,
                )

        except trio.Cancelled:
            log.exception(
                'Cancelled during invoke of internal `debug_func = '
                f'{debug_func.func.__name__}`\n'
            )
            # XXX NOTE: DON'T release lock yet
            raise

        except BaseException:
            __tracebackhide__: bool = False
            log.exception(
                'Failed to invoke internal `debug_func = '
                f'{debug_func.func.__name__}`\n'
            )
            # NOTE: OW this is ONLY called from the
            # `.set_continue/next` hooks!
            DebugStatus.release(cancel_req_task=True)

            raise

    log.devx(
        'Entering `._pause()` for requesting task\n'
        f'|_{task}\n'
    )

    # TODO: this should be created as part of `DebugRequest()` init
    # which should instead be a one-shot-use singleton much like
    # the `PdbREPL`.
    if (
        not DebugStatus.repl_release
        or
        DebugStatus.repl_release.is_set()
    ):
        DebugStatus.repl_release = trio.Event()
    # ^-NOTE-^ this must be created BEFORE scheduling any subactor
    # debug-req task since it needs to wait on it just after
    # `.started()`-ing back its wrapping `.req_cs: CancelScope`.

    repl_err: BaseException|None = None
    try:
        if is_root_process():

            # we also wait in the root-parent for any child that
            # may have the tty locked prior
            # TODO: wait, what about multiple root tasks acquiring it though?
            ctx: Context|None = Lock.ctx_in_debug
            if (
                ctx is None
                and
                DebugStatus.repl
                and
                DebugStatus.repl_task is task
            ):
                # re-entrant root process already has it: noop.
                log.warning(
                    f'This root actor task is already within an active REPL session\n'
                    f'Ignoring this re-entered `tractor.pause()`\n'
                    f'task: {task.name}\n'
                    f'REPL: {Lock.repl}\n'
                    # TODO: use `._frame_stack` scanner to find the @api_frame
                )
                with trio.CancelScope(shield=shield):
                    await trio.lowlevel.checkpoint()
                return

            # XXX: since we need to enter pdb synchronously below,
            # we have to release the lock manually from pdb completion
            # callbacks. Can't think of a nicer way then this atm.
            with trio.CancelScope(shield=shield):
                if Lock._debug_lock.locked():
                    log.warning(
                        'attempting to shield-acquire active TTY lock owned by\n'
                        f'{ctx}'
                    )

                    # must shield here to avoid hitting a ``Cancelled`` and
                    # a child getting stuck bc we clobbered the tty
                    # with trio.CancelScope(shield=True):
                    await Lock._debug_lock.acquire()
                else:
                    # may be cancelled
                    await Lock._debug_lock.acquire()

            # enter REPL from root, no TTY locking IPC ctx necessary
            _enter_repl_sync(debug_func)
            return  # next branch is mutex and for subactors

        # TODO: need a more robust check for the "root" actor
        elif (
            not is_root_process()
            and actor._parent_chan  # a connected child
        ):
            repl_task: Task|None = DebugStatus.repl_task
            req_task: Task|None = DebugStatus.req_task
            if req_task:
                log.warning(
                    f'Already an ongoing repl request?\n'
                    f'|_{req_task}\n\n'

                    f'REPL task is\n'
                    f'|_{repl_task}\n\n'

                )
                # Recurrent entry case.
                # this task already has the lock and is likely
                # recurrently entering a `.pause()`-point either bc,
                # - someone is hacking on runtime internals and put
                #   one inside code that get's called on the way to
                #   this code,
                # - a legit app task uses the 'next' command while in
                #   a REPL sesh, and actually enters another
                #   `.pause()` (in a loop or something).
                #
                # XXX Any other cose is likely a bug.
                if (
                    repl_task
                ):
                    if repl_task is task:
                        log.warning(
                            f'{task.name}@{actor.uid} already has TTY lock\n'
                            f'ignoring..'
                        )
                        with trio.CancelScope(shield=shield):
                            await trio.lowlevel.checkpoint()
                        return

                    else:
                        # if **this** actor is already in debug REPL we want
                        # to maintain actor-local-task mutex access, so block
                        # here waiting for the control to be released - this
                        # -> allows for recursive entries to `tractor.pause()`
                        log.warning(
                            f'{task}@{actor.uid} already has TTY lock\n'
                            f'waiting for release..'
                        )
                        with trio.CancelScope(shield=shield):
                            await DebugStatus.repl_release.wait()
                            await trio.sleep(0.1)

                elif (
                    req_task
                ):
                    log.warning(
                        'Local task already has active debug request\n'
                        f'|_{task}\n\n'

                        'Waiting for previous request to complete..\n'
                    )
                    with trio.CancelScope(shield=shield):
                        await DebugStatus.req_finished.wait()

            # this **must** be awaited by the caller and is done using the
            # root nursery so that the debugger can continue to run without
            # being restricted by the scope of a new task nursery.

            # TODO: if we want to debug a trio.Cancelled triggered exception
            # we have to figure out how to avoid having the service nursery
            # cancel on this task start? I *think* this works below:
            # ```python
            #   actor._service_n.cancel_scope.shield = shield
            # ```
            # but not entirely sure if that's a sane way to implement it?

            # NOTE currently we spawn the lock request task inside this
            # subactor's global `Actor._service_n` so that the
            # lifetime of the lock-request can outlive the current
            # `._pause()` scope while the user steps through their
            # application code and when they finally exit the
            # session, via 'continue' or 'quit' cmds, the `PdbREPL`
            # will manually call `DebugStatus.release()` to release
            # the lock session with the root actor.
            #
            # TODO: ideally we can add a tighter scope for this
            # request task likely by conditionally opening a "debug
            # nursery" inside `_errors_relayed_via_ipc()`, see the
            # todo in tht module, but
            # -[ ] it needs to be outside the normal crash handling
            #   `_maybe_enter_debugger()` block-call.
            # -[ ] we probably only need to allocate the nursery when
            #   we detect the runtime is already in debug mode.
            #
            curr_ctx: Context = current_ipc_ctx()
            # req_ctx: Context = await curr_ctx._debug_tn.start(
            log.devx(
                'Starting request task\n'
                f'|_{task}\n'
            )
            with trio.CancelScope(shield=shield):
                req_ctx: Context = await actor._service_n.start(
                    partial(
                        request_root_stdio_lock,
                        actor_uid=actor.uid,
                        task_uid=(task.name, id(task)),  # task uuid (effectively)
                        shield=shield,
                    )
                )
            # XXX sanity, our locker task should be the one which
            # entered a new IPC ctx with the root actor, NOT the one
            # that exists around the task calling into `._pause()`.
            assert (
                req_ctx
                is
                DebugStatus.req_ctx
                is not
                curr_ctx
            )

            # enter REPL
            _enter_repl_sync(debug_func)

    # TODO: prolly factor this plus the similar block from
    # `_enter_repl_sync()` into a common @cm?
    except BaseException as pause_err:
        if isinstance(pause_err, bdb.BdbQuit):
            log.devx(
                'REPL for pdb was quit!\n'
            )

        # when the actor is mid-runtime cancellation the
        # `Actor._service_n` might get closed before we can spawn
        # the request task, so just ignore expected RTE.
        elif (
            isinstance(pause_err, RuntimeError)
            and
            actor._cancel_called
        ):
            # service nursery won't be usable and we
            # don't want to lock up the root either way since
            # we're in (the midst of) cancellation.
            log.warning(
                'Service nursery likely closed due to actor-runtime cancellation..\n'
                'Ignoring failed debugger lock request task spawn..\n'
            )
            return

        else:
            log.exception(
                'Failed to engage debugger via `_pause()` ??\n'
            )

        DebugStatus.release(cancel_req_task=True)

        # sanity checks for ^ on request/status teardown
        assert DebugStatus.repl is None
        assert DebugStatus.repl_task is None

        # sanity, for when hackin on all this?
        if not isinstance(pause_err, trio.Cancelled):
            req_ctx: Context = DebugStatus.req_ctx
            if req_ctx:
                # XXX, bc the child-task in root might cancel it?
                # assert req_ctx._scope.cancel_called
                assert req_ctx.maybe_error

        raise

    finally:
        # set in finally block of func.. this can be synced-to
        # eventually with a debug_nursery somehow?
        # assert DebugStatus.req_task is None

        # always show frame when request fails due to internal
        # failure in the above code (including an `BdbQuit`).
        if (
            DebugStatus.req_err
            or
            repl_err
        ):
            __tracebackhide__: bool = False


def _set_trace(
    repl: PdbREPL,  # passed by `_pause()`
    hide_tb: bool,

    # partial-ed in by `.pause()`
    api_frame: FrameType,

    # optionally passed in to provide support for
    # `pause_from_sync()` where
    actor: tractor.Actor|None = None,
    task: trio.Task|None = None,
):
    __tracebackhide__: bool = hide_tb
    actor: tractor.Actor = actor or current_actor()
    task: trio.Task = task or current_task()

    # else:
    # TODO: maybe print the actor supervion tree up to the
    # root here? Bo
    log.pdb(
        f'{_pause_msg}\n'
        '|\n'
        # TODO: more compact pformating?
        # -[ ] make an `Actor.__repr()__`
        # -[ ] should we use `log.pformat_task_uid()`?
        f'|_ {task} @ {actor.uid}\n'
    )
    # presuming the caller passed in the "api frame"
    # (the last frame before user code - like `.pause()`)
    # then we only step up one frame to where the user
    # called our API.
    caller_frame: FrameType = api_frame.f_back  # type: ignore

    # engage ze REPL
    # B~()
    repl.set_trace(frame=caller_frame)


async def pause(
    *,
    hide_tb: bool = False,
    api_frame: FrameType|None = None,

    # TODO: figure out how to still make this work:
    # -[ ] pass it direct to `_pause()`?
    # -[ ] use it to set the `debug_nursery.cancel_scope.shield`
    shield: bool = False,
    **_pause_kwargs,

) -> None:
    '''
    A pause point (more commonly known as a "breakpoint") interrupt
    instruction for engaging a blocking debugger instance to
    conduct manual console-based-REPL-interaction from within
    `tractor`'s async runtime, normally from some single-threaded
    and currently executing actor-hosted-`trio`-task in some
    (remote) process.

    NOTE: we use the semantics "pause" since it better encompasses
    the entirety of the necessary global-runtime-state-mutation any
    actor-task must access and lock in order to get full isolated
    control over the process tree's root TTY:
    https://en.wikipedia.org/wiki/Breakpoint

    '''
    __tracebackhide__: bool = hide_tb

    # always start 1 level up from THIS in user code since normally
    # `tractor.pause()` is called explicitly by use-app code thus
    # making it the highest up @api_frame.
    api_frame: FrameType = api_frame or inspect.currentframe()

    # XXX TODO: this was causing cs-stack corruption in trio due to
    # usage within the `Context._scope_nursery` (which won't work
    # based on scoping of it versus call to `_maybe_enter_debugger()`
    # from `._rpc._invoke()`)
    # with trio.CancelScope(
    #     shield=shield,
    # ) as cs:
        # NOTE: so the caller can always manually cancel even
        # if shielded!
        # task_status.started(cs)
        # log.critical(
        #     '`.pause() cancel-scope is:\n\n'
        #     f'{pformat_cs(cs, var_name="pause_cs")}\n\n'
        # )
    await _pause(
        debug_func=partial(
            _set_trace,
            api_frame=api_frame,
        ),
        shield=shield,
        **_pause_kwargs
    )
        # XXX avoid cs stack corruption when `PdbREPL.interaction()`
        # raises `BdbQuit`.
        # await DebugStatus.req_finished.wait()


_gb_mod: None|ModuleType|False = None


def maybe_import_greenback(
    raise_not_found: bool = True,
    force_reload: bool = False,

) -> ModuleType|False:
    # be cached-fast on module-already-inited
    global _gb_mod

    if _gb_mod is False:
        return False

    elif (
        _gb_mod is not None
        and not force_reload
    ):
        return _gb_mod

    try:
        import greenback
        _gb_mod = greenback
        return greenback

    except ModuleNotFoundError as mnf:
        log.debug(
            '`greenback` is not installed.\n'
            'No sync debug support!\n'
        )
        _gb_mod = False

        if raise_not_found:
            raise RuntimeError(
                'The `greenback` lib is required to use `tractor.pause_from_sync()`!\n'
                'https://github.com/oremanj/greenback\n'
            ) from mnf

        return False


async def maybe_init_greenback(
    **kwargs,
) -> None|ModuleType:

    if mod := maybe_import_greenback(**kwargs):
        await mod.ensure_portal()
        log.info(
            '`greenback` portal opened!\n'
            'Sync debug support activated!\n'
        )
        return mod

    return None


# TODO: allow pausing from sync code.
# normally by remapping python's builtin breakpoint() hook to this
# runtime aware version which takes care of all .
def pause_from_sync(

    hide_tb: bool = False,
    # proxied to `_pause()`

    **_pause_kwargs,
    # for eg.
    # shield: bool = False,
    # api_frame: FrameType|None = None,

) -> None:

    __tracebackhide__: bool = hide_tb
    try:
        actor: tractor.Actor = current_actor(
            err_on_no_runtime=False,
        )
        log.debug(
            f'{actor.uid}: JUST ENTERED `tractor.pause_from_sync()`'
            f'|_{actor}\n'
        )
        if not actor:
            raise RuntimeError(
                'Not inside the `tractor`-runtime?\n'
                '`tractor.pause_from_sync()` is not functional without a wrapping\n'
                '- `async with tractor.open_nursery()` or,\n'
                '- `async with tractor.open_root_actor()`\n'
            )

        # NOTE: once supported, remove this AND the one
        # inside `._pause()`!
        if actor.is_infected_aio():
            raise RuntimeError(
                '`tractor.pause[_from_sync]()` not yet supported '
                'for infected `asyncio` mode!'
            )

        # raises on not-found by default
        greenback: ModuleType = maybe_import_greenback()
        mdb: PdbREPL = mk_pdb()

        # run async task which will lock out the root proc's TTY.
        if not DebugStatus.is_main_trio_thread():

            # TODO: we could also check for a non-`.to_thread` context
            # using `trio.from_thread.check_cancelled()` (says
            # oremanj) wherein we get the following outputs:
            #
            # `RuntimeError`: non-`.to_thread` spawned thread
            # noop: non-cancelled `.to_thread`
            # `trio.Cancelled`: cancelled `.to_thread`
            #
            trio.from_thread.run(
                partial(
                    _pause,
                    debug_func=None,
                    repl=mdb,
                    **_pause_kwargs
                ),
            )
            task: threading.Thread = threading.current_thread()

        else:  # we are presumably the `trio.run()` + main thread
            task: trio.Task = current_task()
            greenback.await_(
                _pause(
                    debug_func=None,
                    repl=mdb,
                    **_pause_kwargs,
                )
            )
            DebugStatus.repl_task: str = current_task()

        # TODO: ensure we aggressively make the user aware about
        # entering the global ``breakpoint()`` built-in from sync
        # code?
        _set_trace(
            api_frame=inspect.currentframe(),
            repl=mdb,
            hide_tb=hide_tb,
            actor=actor,
            task=task,
        )
        # LEGACY NOTE on next LOC's frame showing weirdness..
        #
        # XXX NOTE XXX no other LOC can be here without it
        # showing up in the REPL's last stack frame !?!
        # -[ ] tried to use `@pdbp.hideframe` decoration but
        #   still doesn't work
    except BaseException as err:
        __tracebackhide__: bool = False
        raise err


# NOTE prefer a new "pause" semantic since it better describes
# "pausing the actor's runtime" for this particular
# paralell task to do debugging in a REPL.
async def breakpoint(
    hide_tb: bool = True,
    **kwargs,
):
    log.warning(
        '`tractor.breakpoint()` is deprecated!\n'
        'Please use `tractor.pause()` instead!\n'
    )
    __tracebackhide__: bool = hide_tb
    await pause(
        api_frame=inspect.currentframe(),
        **kwargs,
    )


_crash_msg: str = (
    'Attaching to pdb REPL in crashed actor'
)


def _post_mortem(
    # provided and passed by `_pause()`
    repl: PdbREPL,

    # XXX all `partial`-ed in by `post_mortem()` below!
    tb: TracebackType,
    api_frame: FrameType,

    shield: bool = False,
    hide_tb: bool = False,

) -> None:
    '''
    Enter the ``pdbpp`` port mortem entrypoint using our custom
    debugger instance.

    '''
    __tracebackhide__: bool = hide_tb
    actor: tractor.Actor = current_actor()

    # TODO: print the actor supervion tree up to the root
    # here! Bo
    log.pdb(
        f'{_crash_msg}\n'
        '|\n'
        # f'|_ {current_task()}\n'
        f'|_ {current_task()} @ {actor.uid}\n'

        # f'|_ @{actor.uid}\n'
        # TODO: make an `Actor.__repr()__`
        # f'|_ {current_task()} @ {actor.name}\n'
    )

    # NOTE only replacing this from `pdbp.xpm()` to add the
    # `end=''` to the print XD
    print(traceback.format_exc(), end='')

    caller_frame: FrameType = api_frame.f_back

    # NOTE: see the impl details of followings to understand usage:
    # - `pdbp.post_mortem()`
    # - `pdbp.xps()`
    # - `bdb.interaction()`
    repl.reset()
    repl.interaction(
        frame=caller_frame,
        # frame=None,
        traceback=tb,
    )
    # XXX NOTE XXX: absolutely required to avoid hangs!
    # Since we presume the post-mortem was enaged to a task-ending
    # error, we MUST release the local REPL request so that not other
    # local task nor the root remains blocked!
    DebugStatus.release()


async def post_mortem(
    *,
    tb: TracebackType|None = None,
    api_frame: FrameType|None = None,
    hide_tb: bool = False,

    # TODO: support shield here just like in `pause()`?
    # shield: bool = False,

    **_pause_kwargs,

) -> None:
    '''
    `tractor`'s builtin async equivalient of `pdb.post_mortem()`
    which can be used inside exception handlers.

    It's also used for the crash handler when `debug_mode == True` ;)

    '''
    __tracebackhide__: bool = hide_tb

    tb: TracebackType = tb or sys.exc_info()[2]

    # TODO: do upward stack scan for highest @api_frame and
    # use its parent frame as the expected user-app code
    # interact point.
    api_frame: FrameType = api_frame or inspect.currentframe()

    await _pause(
        debug_func=partial(
            _post_mortem,
            api_frame=api_frame,
            tb=tb,
        ),
        hide_tb=hide_tb,
        **_pause_kwargs
    )


async def _maybe_enter_pm(
    err: BaseException,
    *,
    tb: TracebackType|None = None,
    api_frame: FrameType|None = None,
    hide_tb: bool = False,
):
    from tractor._exceptions import is_multi_cancelled
    if (
        debug_mode()

        # NOTE: don't enter debug mode recursively after quitting pdb
        # Iow, don't re-enter the repl if the `quit` command was issued
        # by the user.
        and not isinstance(err, bdb.BdbQuit)

        # XXX: if the error is the likely result of runtime-wide
        # cancellation, we don't want to enter the debugger since
        # there's races between when the parent actor has killed all
        # comms and when the child tries to contact said parent to
        # acquire the tty lock.

        # Really we just want to mostly avoid catching KBIs here so there
        # might be a simpler check we can do?
        and not is_multi_cancelled(err)
    ):
        api_frame: FrameType = api_frame or inspect.currentframe()
        tb: TracebackType = tb or sys.exc_info()[2]
        await post_mortem(
            api_frame=api_frame,
            tb=tb,
        )
        return True

    else:
        return False


@acm
async def acquire_debug_lock(
    subactor_uid: tuple[str, str],
) -> AsyncGenerator[
    trio.CancelScope|None,
    tuple,
]:
    '''
    Request to acquire the TTY `Lock` in the root actor, release on exit.

    This helper is for actor's who don't actually need to acquired
    the debugger but want to wait until the lock is free in the
    process-tree root such that they don't clobber an ongoing pdb
    REPL session in some peer or child!

    '''
    if not debug_mode():
        yield None
        return

    async with trio.open_nursery() as n:
        ctx: Context = await n.start(
            request_root_stdio_lock,
            subactor_uid,
        )
        yield ctx
        ctx.cancel()


async def maybe_wait_for_debugger(
    poll_steps: int = 2,
    poll_delay: float = 0.1,
    child_in_debug: bool = False,

    header_msg: str = '',
    _ll: str = 'devx',

) -> bool:  # was locked and we polled?

    if (
        not debug_mode()
        and not child_in_debug
    ):
        return False

    logmeth: Callable = getattr(log, _ll)

    msg: str = header_msg
    if (
        is_root_process()
    ):
        # If we error in the root but the debugger is
        # engaged we don't want to prematurely kill (and
        # thus clobber access to) the local tty since it
        # will make the pdb repl unusable.
        # Instead try to wait for pdb to be released before
        # tearing down.
        ctx_in_debug: Context|None = Lock.ctx_in_debug
        in_debug: tuple[str, str]|None = (
            ctx_in_debug.chan.uid
            if ctx_in_debug
            else None
        )
        if in_debug == current_actor().uid:
            log.debug(
                msg
                +
                'Root already owns the TTY LOCK'
            )
            return True

        elif in_debug:
            msg += (
                f'Debug `Lock` in use by subactor\n|\n|_{in_debug}\n'
            )
            # TODO: could this make things more deterministic?
            # wait to see if a sub-actor task will be
            # scheduled and grab the tty lock on the next
            # tick?
            # XXX => but it doesn't seem to work..
            # await trio.testing.wait_all_tasks_blocked(cushion=0)
        else:
            logmeth(
                msg
                +
                'Root immediately acquired debug TTY LOCK'
            )
            return False

        for istep in range(poll_steps):
            if (
                Lock.req_handler_finished is not None
                and not Lock.req_handler_finished.is_set()
                and in_debug is not None
            ):

                # caller_frame_info: str = pformat_caller_frame()
                logmeth(
                    msg
                    +
                    '\nRoot is waiting on tty lock to release from\n\n'
                    # f'{caller_frame_info}\n'
                )

                if not any_connected_locker_child():
                    Lock.get_locking_task_cs().cancel()

                with trio.CancelScope(shield=True):
                    await Lock.req_handler_finished.wait()

                log.pdb(
                    f'Subactor released debug lock\n'
                    f'|_{in_debug}\n'
                )
                break

            # is no subactor locking debugger currently?
            if (
                in_debug is None
                and (
                    Lock.req_handler_finished is None
                    or Lock.req_handler_finished.is_set()
                )
            ):
                logmeth(
                    msg
                    +
                    'Root acquired tty lock!'
                )
                break

            else:
                logmeth(
                    'Root polling for debug:\n'
                    f'poll step: {istep}\n'
                    f'poll delya: {poll_delay}\n\n'
                    f'{Lock.repr()}\n'
                )
                with CancelScope(shield=True):
                    await trio.sleep(poll_delay)
                    continue

        # fallthrough on failure to acquire..
        # else:
        #     raise RuntimeError(
        #         msg
        #         +
        #         'Root actor failed to acquire debug lock?'
        #     )
        return True

    # else:
    #     # TODO: non-root call for #320?
    #     this_uid: tuple[str, str] = current_actor().uid
    #     async with acquire_debug_lock(
    #         subactor_uid=this_uid,
    #     ):
    #         pass
    return False

# TODO: better naming and what additionals?
# - [ ] optional runtime plugging?
# - [ ] detection for sync vs. async code?
# - [ ] specialized REPL entry when in distributed mode?
# - [x] allow ignoring kbi Bo
@cm
def open_crash_handler(
    catch: set[BaseException] = {
        Exception,
        BaseException,
    },
    ignore: set[BaseException] = {
        KeyboardInterrupt,
    },
):
    '''
    Generic "post mortem" crash handler using `pdbp` REPL debugger.

    We expose this as a CLI framework addon to both `click` and
    `typer` users so they can quickly wrap cmd endpoints which get
    automatically wrapped to use the runtime's `debug_mode: bool`
    AND `pdbp.pm()` around any code that is PRE-runtime entry
    - any sync code which runs BEFORE the main call to
      `trio.run()`.

    '''
    try:
        yield
    except tuple(catch) as err:

        if type(err) not in ignore:
            pdbp.xpm()

        raise


@cm
def maybe_open_crash_handler(pdb: bool = False):
    '''
    Same as `open_crash_handler()` but with bool input flag
    to allow conditional handling.

    Normally this is used with CLI endpoints such that if the --pdb
    flag is passed the pdb REPL is engaed on any crashes B)
    '''
    rtctx = nullcontext
    if pdb:
        rtctx = open_crash_handler

    with rtctx():
        yield
