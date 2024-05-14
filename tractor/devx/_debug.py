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
    FrameType,
    ModuleType,
    TracebackType,
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
from .pformat import (
    # pformat_caller_frame,
    pformat_cs,
)

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
pdbp.hideframe(trio._core._run.NurseryManager.__aexit__)
pdbp.hideframe(trio._core._run.CancelScope.__exit__)
pdbp.hideframe(_GeneratorContextManager.__exit__)
pdbp.hideframe(_AsyncGeneratorContextManager.__aexit__)
pdbp.hideframe(trio.Event.wait)

__all__ = [
    'breakpoint',
    'post_mortem',
]


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

    no_remote_has_tty: trio.Event|None = None
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
                f'no_remote_has_tty: {cls.no_remote_has_tty}\n'
                f'_blocked: {cls._blocked}\n\n'

                f'ctx_in_debug: {cls.ctx_in_debug}\n\n'

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
            ')>'
        )

    @classmethod
    @pdbp.hideframe
    def release(
        cls,
        force: bool = False,
    ):
        try:
            lock: trio.StrictFIFOLock = cls._debug_lock
            owner: Task = lock.statistics().owner
            if (
                lock.locked()
                and
                owner is current_task()
                # ^-NOTE-^ if not will raise a RTE..
            ):
                if not DebugStatus.is_main_trio_thread():
                    trio.from_thread.run_sync(
                        cls._debug_lock.release
                    )
                else:
                    cls._debug_lock.release()
                    message: str = 'TTY lock released for child\n'

            else:
                message: str = 'TTY lock not held by any child\n'

        finally:
            # IFF there are no more requesting tasks queued up fire, the
            # "tty-unlocked" event thereby alerting any monitors of the lock that
            # we are now back in the "tty unlocked" state. This is basically
            # and edge triggered signal around an empty queue of sub-actor
            # tasks that may have tried to acquire the lock.
            stats = cls._debug_lock.statistics()
            if (
                not stats.owner
                or force
                # and cls.no_remote_has_tty is not None
            ):
                message += '-> No more child ctx tasks hold the TTY lock!\n'

                # set and release
                if cls.no_remote_has_tty is not None:
                    cls.no_remote_has_tty.set()
                    cls.no_remote_has_tty = None

                    # cls.remote_task_in_debug = None

                else:
                    message += (
                        f'-> Not signalling `Lock.no_remote_has_tty` since it has value:{cls.no_remote_has_tty}\n'
                    )

            else:
                # wakeup any waiters since the lock was released
                # (presumably) temporarily.
                if no_remote_has_tty := cls.no_remote_has_tty:
                    no_remote_has_tty.set()
                    no_remote_has_tty = trio.Event()

                message += (
                    f'-> A child ctx task still owns the `Lock` ??\n'
                    f'   |_owner task: {stats.owner}\n'
                )

            cls.ctx_in_debug = None

    @classmethod
    @acm
    async def acquire(
        cls,
        ctx: Context,
        # subactor_uid: tuple[str, str],
        # remote_task_uid: str,

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
                # and cls.no_remote_has_tty is not None
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
            if cls.no_remote_has_tty is None:
                # mark the tty lock as being in use so that the runtime
                # can try to avoid clobbering any connection from a child
                # that's currently relying on it.
                cls.no_remote_has_tty = trio.Event()
                # cls.remote_task_in_debug = remote_task_uid

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
            if (
                we_acquired
                # and
                # cls._debug_lock.locked()
            ):
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
    # NOTE: we use the IPC ctx's cancel scope directly in order to
    # ensure that on any transport failure, or cancellation request
    # from the child we expect
    # `Context._maybe_cancel_and_set_remote_error()` to cancel this
    # scope despite the shielding we apply below.
    debug_lock_cs: CancelScope = ctx._scope

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
            await ctx.cancel(f'Debug lock blocked for {subactor_uid}')
            # TODO: remove right?
            # return LockStatus(
            #     subactor_uid=subactor_uid,
            #     cid=ctx.cid,
            #     locked=False,
            # )

        # TODO: when we get to true remote debugging
        # this will deliver stdin data?

        log.debug(
            'Subactor attempting to acquire TTY lock\n'
            f'root task: {root_task_name}\n'
            f'subactor_uid: {subactor_uid}\n'
            f'remote task: {subactor_task_uid}\n'
        )
        DebugStatus.shield_sigint()
        Lock._blocked.add(ctx.cid)
        with (
            # enable the locking msgspec
            apply_debug_pldec(),
        ):
            async with Lock.acquire(ctx=ctx):
                debug_lock_cs.shield = True

                # indicate to child that we've locked stdio
                await ctx.started(
                    LockStatus(
                        subactor_uid=subactor_uid,
                        cid=ctx.cid,
                        locked=True,
                    )
                )

                log.debug( f'Actor {subactor_uid} acquired TTY lock')

                # wait for unlock pdb by child
                async with ctx.open_stream() as stream:
                    release_msg: LockRelease = await stream.receive()

                    # TODO: security around only releasing if
                    # these match?
                    log.pdb(
                        f'TTY lock released requested\n\n'
                        f'{release_msg}\n'
                    )
                    assert release_msg.cid == ctx.cid
                    assert release_msg.subactor_uid == tuple(subactor_uid)

                log.debug(f'Actor {subactor_uid} released TTY lock')

            return LockStatus(
                subactor_uid=subactor_uid,
                cid=ctx.cid,
                locked=False,
            )

    except BaseException as req_err:
        message: str = (
            'Forcing `Lock.release()` since likely an internal error!\n'
        )
        if isinstance(req_err, trio.Cancelled):
            log.cancel(
                'Cancelled during root TTY-lock dialog?\n'
                +
                message
            )
        else:
            log.exception(
                'Errored during root TTY-lock dialog?\n'
                +
                message
            )

        Lock.release(force=True)
        raise

    finally:
        Lock._blocked.remove(ctx.cid)
        if (no_locker := Lock.no_remote_has_tty):
            no_locker.set()

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
    repl_task: Task|None = None
    req_ctx: Context|None = None
    req_cs: CancelScope|None = None
    repl_release: trio.Event|None = None
    req_finished: trio.Event|None = None
    lock_status: LockStatus|None = None
    req_err: BaseException|None = None

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

            # restore original sigint handler
            cls.unshield_sigint()

            # actor-local state, irrelevant for non-root.
            cls.repl_task = None
            cls.repl = None


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

    # TODO: special handling where we just want the next LOC and
    # not to resume to the next pause/crash point?
    # def set_next(
    #     self,
    #     frame: FrameType
    # ) -> None:
    #     try:
    #         super().set_next(frame)
    #     finally:
    #         pdbp.set_trace()

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


@cm
def apply_debug_pldec() -> _codec.MsgCodec:
    '''
    Apply the subactor TTY `Lock`-ing protocol's msgspec temporarily
    (only in the current task).

    '''
    from tractor.msg import (
        _ops as msgops,
    )
    orig_plrx: msgops.PldRx = msgops.current_pldrx()
    orig_pldec: msgops.MsgDec = orig_plrx.pld_dec

    try:
        with msgops.limit_plds(
            spec=__pld_spec__,
        ) as debug_dec:
            assert (
                debug_dec
                is
                msgops.current_pldrx().pld_dec
            )
            log.info(
                'Applied `.devx._debug` pld-spec\n\n'
                f'{debug_dec}\n'
            )
            yield debug_dec

    finally:
        assert (
            (plrx := msgops.current_pldrx()) is orig_plrx
            and
            plrx.pld_dec is orig_pldec
        )
        log.info(
            'Reverted to previous pld-spec\n\n'
            f'{orig_pldec}\n'
        )


async def request_root_stdio_lock(
    actor_uid: tuple[str, str],
    task_uid: tuple[str, int],
    task_status: TaskStatus[CancelScope] = trio.TASK_STATUS_IGNORED
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

    log.pdb(
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
    try:
        from tractor._discovery import get_root
        from tractor.msg import _ops as msgops
        debug_dec: msgops.MsgDec
        with (
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
            trio.CancelScope(shield=True) as req_cs,

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
            #
            apply_debug_pldec() as debug_dec,
        ):
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
                    ) as (req_ctx, status):

                        DebugStatus.req_ctx = req_ctx

                        # sanity checks on pld-spec limit state
                        assert debug_dec
                        # curr_pldrx: msgops.PldRx = msgops.current_pldrx()
                        # assert (
                        #     curr_pldrx.pld_dec is debug_dec
                        # )

                        log.debug(
                            'Subactor locked TTY with msg\n\n'
                            f'{status}\n'
                        )

                        # mk_pdb().set_trace()
                        try:
                            assert status.subactor_uid == actor_uid
                            assert status.cid
                        except AttributeError:
                            log.exception('failed pldspec asserts!')
                            raise

                        # set last rxed lock dialog status.
                        DebugStatus.lock_status = status

                        async with req_ctx.open_stream() as stream:

                            assert DebugStatus.repl_release
                            task_status.started(req_ctx)

                            # wait for local task to exit its
                            # `PdbREPL.interaction()`, call
                            # `DebugStatus.release()` and then
                            # unblock here.
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

                    log.pdb(
                        'TTY lock was released for subactor with msg\n\n'
                        f'{status}\n\n'
                        f'Exitting {req_ctx.side!r}-side of locking req_ctx'
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
            ):
                log.exception(
                    'Failed during root TTY-lock dialog?\n'
                    f'{req_ctx}\n'

                    f'Cancelling IPC ctx!\n'
                )
                await req_ctx.cancel()
                raise


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
            f'req_ctx: {req_ctx}\n'
        ) from req_err

    finally:
        log.debug('Exiting debugger TTY lock request func from child')
        # signal request task exit
        DebugStatus.req_finished.set()


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

        task: str|None = DebugStatus.repl_task
        if (
            task
            and
            repl
        ):
            log.pdb(
                f'Ignoring SIGINT while local task using debug REPL\n'
                f'|_{task}\n'
                f'  |_{repl}\n'
            )
        else:
            msg: str = (
                'SIGINT shield handler still active BUT, \n\n'
            )
            if task is None:
                msg += (
                    f'- No local task claims to be in debug?\n'
                    f' |_{task}\n\n'
                )

            if repl is None:
                msg += (
                    f'- No local REPL is currently active?\n'
                    f' |_{repl}\n\n'
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
            # raise KeyboardInterrupt

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
    # log.warning('exiting SIGINT')


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
    # shield: bool = False,
    hide_tb: bool = True,

    # bc, `debug_func()`, `_enter_repl_sync()` and `_pause()`
    # extra_frames_up_when_async: int = 3,

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

    # TODO: this should be created as part of `DebugRequest()` init
    # which should instead be a one-shot-use singleton much like
    # the `PdbREPL`.
    if (
        not DebugStatus.repl_release
        or
        DebugStatus.repl_release.is_set()
    ):
        DebugStatus.repl_release = trio.Event()

    if debug_func is not None:
        debug_func = partial(debug_func)

    repl: PdbREPL = repl or mk_pdb()

    # TODO: maybe make this a `PdbREPL` method or mod func?
    # -[ ] factor out better, main reason for it is common logic for
    #   both root and sub repl entry
    def _enter_repl_sync(
        debug_func: Callable,
    ) -> None:
        __tracebackhide__: bool = hide_tb

        # TODO: do we want to support using this **just** for the
        # locking / common code (prolly to help address #320)?
        #
        if debug_func is None:
            task_status.started(DebugStatus)
        else:
            # block here one (at the appropriate frame *up*) where
            # ``breakpoint()`` was awaited and begin handling stdio.
            log.debug('Entering sync world of the `pdb` REPL..')

            # XXX used by the SIGINT handler to check if
            # THIS actor is in REPL interaction
            try:
                # TODO: move this into a `open_debug_request()` @acm?
                # -[ ] prolly makes the most send to do the request
                #   task spawn as part of an `@acm` api which
                #   delivers the `DebugRequest` instance and ensures
                #   encapsing all the pld-spec and debug-nursery?
                #
                # set local actor task to avoid recurrent
                # entries/requests from the same local task
                # (to the root process).
                DebugStatus.repl_task = task
                DebugStatus.repl = repl
                DebugStatus.shield_sigint()

                # enter `PdbREPL` specific method
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
                # NOTE: DON'T release lock yet
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
                await trio.lowlevel.checkpoint()
                return

            # XXX: since we need to enter pdb synchronously below,
            # we have to release the lock manually from pdb completion
            # callbacks. Can't think of a nicer way then this atm.
            if Lock._debug_lock.locked():
                log.warning(
                    'attempting to shield-acquire active TTY lock owned by\n'
                    f'{ctx}'
                )

                # must shield here to avoid hitting a ``Cancelled`` and
                # a child getting stuck bc we clobbered the tty
                with trio.CancelScope(shield=True):
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
            if DebugStatus.repl_task:

                # Recurrence entry case: this task already has the lock and
                # is likely recurrently entering a breakpoint
                #
                # NOTE: noop on recurrent entry case but we want to trigger
                # a checkpoint to allow other actors error-propagate and
                # potetially avoid infinite re-entries in some
                # subactor that would otherwise not bubble until the
                # next checkpoint was hit.
                if (
                    (repl_task := DebugStatus.repl_task)
                    and
                    repl_task is task
                ):
                    log.warning(
                        f'{task.name}@{actor.uid} already has TTY lock\n'
                        f'ignoring..'
                    )
                    await trio.lowlevel.checkpoint()
                    return

                # if **this** actor is already in debug REPL we want
                # to maintain actor-local-task mutex access, so block
                # here waiting for the control to be released - this
                # -> allows for recursive entries to `tractor.pause()`
                log.warning(
                    f'{task.name}@{actor.uid} already has TTY lock\n'
                    f'waiting for release..'
                )
                await DebugStatus.repl_release.wait()
                await trio.sleep(0.1)

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
            # ctx: Context = await curr_ctx._debug_tn.start(
            req_ctx: Context = await actor._service_n.start(
                request_root_stdio_lock,
                actor.uid,
                (task.name, id(task)),  # task uuid (effectively)
            )
            # XXX sanity, our locker task should be the one which
            # entered a new IPC ctx with the root actor, NOT the one
            # that exists around the task calling into `._pause()`.
            curr_ctx: Context = current_ipc_ctx()
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
    except BaseException as repl_err:
        if isinstance(repl_err, bdb.BdbQuit):
            log.devx(
                'REPL for pdb was quit!\n'
            )

        # when the actor is mid-runtime cancellation the
        # `Actor._service_n` might get closed before we can spawn
        # the request task, so just ignore expected RTE.
        elif (
            isinstance(repl_err, RuntimeError)
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
        req_ctx: Context = DebugStatus.req_ctx
        if req_ctx:
            assert req_ctx._scope.cancel_called

        raise

    finally:
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
):
    __tracebackhide__: bool = hide_tb
    actor: tractor.Actor = current_actor()

    # else:
    # TODO: maybe print the actor supervion tree up to the
    # root here? Bo
    log.pdb(
        f'{_pause_msg}\n'
        '|\n'
        # TODO: make an `Actor.__repr()__`
        f'|_ {current_task()} @ {actor.uid}\n'
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
    hide_tb: bool = True,
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

        # task_status=task_status,
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
) -> None:

    __tracebackhide__: bool = hide_tb
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
    if not Lock.is_main_trio_thread():

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
                pause,
                debug_func=None,
                pdb=mdb,
                hide_tb=hide_tb,
            )
        )
        # TODO: maybe the `trio.current_task()` id/name if avail?
        DebugStatus.repl_task: str = str(threading.current_thread())

    else:  # we are presumably the `trio.run()` + main thread
        greenback.await_(
            pause(
                debug_func=None,
                pdb=mdb,
                hide_tb=hide_tb,
            )
        )
        DebugStatus.repl_task: str = current_task()

    # TODO: ensure we aggressively make the user aware about
    # entering the global ``breakpoint()`` built-in from sync
    # code?
    _set_trace(
        api_frame=inspect.current_frame(),
        actor=actor,
        pdb=mdb,
        hide_tb=hide_tb,

        # TODO? will we ever need it?
        # -> the gb._await() won't be affected by cancellation?
        # shield=shield,
    )
    # LEGACY NOTE on next LOC's frame showing weirdness..
    #
    # XXX NOTE XXX no other LOC can be here without it
    # showing up in the REPL's last stack frame !?!
    # -[ ] tried to use `@pdbp.hideframe` decoration but
    #   still doesn't work


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

) -> bool:  # was locked and we polled?

    if (
        not debug_mode()
        and not child_in_debug
    ):
        return False


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
        in_debug: tuple[str, str]|None = ctx_in_debug.chan.uid if ctx_in_debug else None
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
            log.debug(
                msg
                +
                'Root immediately acquired debug TTY LOCK'
            )
            return False

        for istep in range(poll_steps):
            if (
                Lock.no_remote_has_tty is not None
                and not Lock.no_remote_has_tty.is_set()
                and in_debug is not None
            ):

                # caller_frame_info: str = pformat_caller_frame()
                log.debug(
                    msg
                    +
                    '\nRoot is waiting on tty lock to release from\n\n'
                    # f'{caller_frame_info}\n'
                )

                if not any_connected_locker_child():
                    Lock.get_locking_task_cs().cancel()

                with trio.CancelScope(shield=True):
                    await Lock.no_remote_has_tty.wait()

                log.pdb(
                    f'Subactor released debug lock\n'
                    f'|_{in_debug}\n'
                )
                break

            # is no subactor locking debugger currently?
            if (
                in_debug is None
                and (
                    Lock.no_remote_has_tty is None
                    or Lock.no_remote_has_tty.is_set()
                )
            ):
                log.pdb(
                    msg
                    +
                    'Root acquired tty lock!'
                )
                break

            else:
                # TODO: don't need this right?
                # await trio.lowlevel.checkpoint()

                log.debug(
                    'Root polling for debug:\n'
                    f'poll step: {istep}\n'
                    f'poll delya: {poll_delay}'
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
