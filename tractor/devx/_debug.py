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
)
from functools import (
    partial,
    cached_property,
)
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
)

from msgspec import Struct
import pdbp
import sniffio
import tractor
import trio
from trio.lowlevel import (
    current_task,
    Task,
)
from trio import (
    TaskStatus,
)

from tractor.log import get_logger
from tractor.msg import (
    _codec,
)
from tractor._state import (
    current_actor,
    is_root_process,
    debug_mode,
)
from tractor._exceptions import (
    is_multi_cancelled,
    ContextCancelled,
)
from tractor._ipc import Channel

if TYPE_CHECKING:
    from tractor._runtime import (
        Actor,
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


__msg_spec__: TypeAlias = LockStatus|LockRelease


class Lock:
    '''
    Actor global debug lock state.

    Mostly to avoid a lot of ``global`` declarations for now XD.

    '''
    # XXX local ref to the `Pbp` instance, ONLY set in the
    # actor-process that currently has activated a REPL
    # i.e. it will be `None` (unset) in any other actor-process
    # that does not have this lock acquired in the root proc.
    repl: PdbREPL|None = None

    # placeholder for function to set a ``trio.Event`` on debugger exit
    # pdb_release_hook: Callable | None = None

    remote_task_in_debug: str|None = None

    @staticmethod
    def get_locking_task_cs() -> trio.CancelScope|None:
        if is_root_process():
            return Lock._locking_task_cs

        raise RuntimeError(
            '`Lock.locking_task_cs` is invalid in subactors!'
        )

    @staticmethod
    def set_locking_task_cs(
        cs: trio.CancelScope,
    ) -> None:
        if not is_root_process():
            raise RuntimeError(
                '`Lock.locking_task_cs` is invalid in subactors!'
            )

        Lock._locking_task_cs = cs

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
    global_actor_in_debug: tuple[str, str]|None = None
    no_remote_has_tty: trio.Event|None = None
    _locking_task_cs: trio.CancelScope|None = None

    _debug_lock: trio.StrictFIFOLock = trio.StrictFIFOLock()
    _blocked: set[tuple[str, str]] = set()  # `Actor.uid` block list

    @classmethod
    def repr(cls) -> str:

        # both root and subs
        fields: str = (
            f'repl: {cls.repl}\n'
        )

        if is_root_process():
            lock_stats: trio.LockStatistics = cls._debug_lock.statistics()
            fields += (
                f'global_actor_in_debug: {cls.global_actor_in_debug}\n'
                f'no_remote_has_tty: {cls.no_remote_has_tty}\n'
                f'remote_task_in_debug: {cls.remote_task_in_debug}\n'
                f'_locking_task_cs: {cls.get_locking_task_cs()}\n'
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
            ')>'
        )

    @classmethod
    def release(cls):
        try:
            if not DebugStatus.is_main_trio_thread():
                trio.from_thread.run_sync(
                    cls._debug_lock.release
                )
            else:
                cls._debug_lock.release()

        except RuntimeError as rte:
            # uhhh makes no sense but been seeing the non-owner
            # release error even though this is definitely the task
            # that locked?
            owner = cls._debug_lock.statistics().owner
            # if (
            #     owner
            #     and
            #     cls.remote_task_in_debug is None
            # ):
            #     raise RuntimeError(
            #         'Stale `Lock` detected, no remote task active!?\n'
            #         f'|_{owner}\n'
            #         # f'{Lock}'
            #     ) from rte

            if owner:
                raise rte

            # OW suppress, can't member why tho .. XD
            # something somethin corrupts a cancel-scope
            # somewhere..

        try:
            # sometimes the ``trio`` might already be terminated in
            # which case this call will raise.
            if DebugStatus.repl_release is not None:
                DebugStatus.repl_release.set()

        finally:
            cls.repl = None
            cls.global_actor_in_debug = None

            # restore original sigint handler
            DebugStatus.unshield_sigint()
            # actor-local state, irrelevant for non-root.
            DebugStatus.repl_task = None


# TODO: actually use this instead throughout for subs!
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
    req_cs: trio.CancelScope|None = None
    repl_release: trio.Event|None = None

    lock_status: LockStatus|None = None

    _orig_sigint_handler: Callable | None = None
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
            f'req_cs: {cls.req_cs}\n'
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
    def shield_sigint(cls):
        '''
        Shield out SIGINT handling (which by default triggers
        `trio.Task` cancellation) in subactors when the `pdb` REPL
        is active.

        Avoids cancellation of the current actor (task) when the
        user mistakenly sends ctl-c or a signal is received from
        an external request; explicit runtime cancel requests are
        allowed until the use exits the REPL session using
        'continue' or 'quit', at which point the orig SIGINT
        handler is restored.

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
            Lock.release()

    def set_quit(self):
        try:
            super().set_quit()
        finally:
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
    #         Lock.release()

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


@acm
async def _acquire_debug_lock_from_root_task(
    subactor_uid: tuple[str, str],
    remote_task_uid: str,

) -> AsyncIterator[trio.StrictFIFOLock]:
    '''
    Acquire a root-actor local FIFO lock which tracks mutex access of
    the process tree's global debugger breakpoint.

    This lock avoids tty clobbering (by preventing multiple processes
    reading from stdstreams) and ensures multi-actor, sequential access
    to the ``pdb`` repl.

    '''
    # task_name: str = current_task().name
    we_acquired: bool = False

    log.runtime(
        f'Attempting to acquire TTY lock for,\n'
        f'subactor_uid: {subactor_uid}\n'
        f'remote task: {remote_task_uid}\n'
    )
    try:
        pre_msg: str = (
            f'Entering lock checkpoint for sub-actor\n'
            f'subactor_uid: {subactor_uid}\n'
            f'remote task: {remote_task_uid}\n'
        )
        stats = Lock._debug_lock.statistics()
        if owner := stats.owner:
            # and Lock.no_remote_has_tty is not None
            pre_msg += (
                f'\n'
                f'`Lock` already held by local task\n'
                f'{owner}\n\n'
                f'On behalf of remote task: {Lock.remote_task_in_debug!r}\n'
            )
        log.runtime(pre_msg)

        # NOTE: if the surrounding cancel scope from the
        # `lock_tty_for_child()` caller is cancelled, this line should
        # unblock and NOT leave us in some kind of
        # a "child-locked-TTY-but-child-is-uncontactable-over-IPC"
        # condition.
        await Lock._debug_lock.acquire()
        we_acquired = True

        if Lock.no_remote_has_tty is None:
            # mark the tty lock as being in use so that the runtime
            # can try to avoid clobbering any connection from a child
            # that's currently relying on it.
            Lock.no_remote_has_tty = trio.Event()
            Lock.remote_task_in_debug = remote_task_uid

        Lock.global_actor_in_debug = subactor_uid
        log.runtime(
            f'TTY lock acquired for,\n'
            f'subactor_uid: {subactor_uid}\n'
            f'remote task: {remote_task_uid}\n'
        )

        # NOTE: critical section: this yield is unshielded!

        # IF we received a cancel during the shielded lock entry of some
        # next-in-queue requesting task, then the resumption here will
        # result in that ``trio.Cancelled`` being raised to our caller
        # (likely from ``lock_tty_for_child()`` below)!  In
        # this case the ``finally:`` below should trigger and the
        # surrounding caller side context should cancel normally
        # relaying back to the caller.

        yield Lock._debug_lock

    finally:
        if (
            we_acquired
            and
            Lock._debug_lock.locked()
        ):
            Lock._debug_lock.release()

        # IFF there are no more requesting tasks queued up fire, the
        # "tty-unlocked" event thereby alerting any monitors of the lock that
        # we are now back in the "tty unlocked" state. This is basically
        # and edge triggered signal around an empty queue of sub-actor
        # tasks that may have tried to acquire the lock.
        stats = Lock._debug_lock.statistics()
        if (
            not stats.owner
            # and Lock.no_remote_has_tty is not None
        ):
            # log.runtime(
            log.info(
                f'No more child ctx tasks hold the TTY lock!\n'
                f'last subactor: {subactor_uid}\n'
                f'remote task: {remote_task_uid}\n'
            )
            if Lock.no_remote_has_tty is not None:
                # set and release
                Lock.no_remote_has_tty.set()
                Lock.no_remote_has_tty = None
                Lock.remote_task_in_debug = None
            else:
                log.warning(
                    'Not signalling `Lock.no_remote_has_tty` since it has value:\n'
                    f'{Lock.no_remote_has_tty}\n'
                )
        else:
            log.info(
                f'A child ctx tasks still holds the TTY lock ??\n'
                f'last subactor: {subactor_uid}\n'
                f'remote task: {remote_task_uid}\n'
                f'current local owner task: {stats.owner}\n'
            )

        Lock.global_actor_in_debug = None
        log.runtime(
            'TTY lock released by child\n'
            f'last subactor: {subactor_uid}\n'
            f'remote task: {remote_task_uid}\n'
        )


@tractor.context
async def lock_tty_for_child(

    ctx: tractor.Context,

    # TODO: when we finally get a `Start.params: ParamSpec`
    # working it'd sure be nice to have `msgspec` auto-decode this
    # to an actual tuple XD
    subactor_uid: tuple[str, str],
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
    req_task_uid: tuple = tuple(subactor_task_uid)
    if req_task_uid in Lock._blocked:
        raise RuntimeError(
            f'Double lock request!?\n'
            f'The same remote task already has an active request for TTY lock ??\n\n'
            f'task uid: {req_task_uid}\n'
            f'subactor uid: {subactor_uid}\n\n'

            'This might be mean that the requesting task '
            'in `wait_for_parent_stdin_hijack()` may have crashed?\n'
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
        return LockStatus(
            subactor_uid=subactor_uid,
            cid=ctx.cid,
            locked=False,
        )

    # TODO: when we get to true remote debugging
    # this will deliver stdin data?

    log.debug(
        'Subactor attempting to acquire TTY lock\n'
        f'root task: {root_task_name}\n'
        f'subactor_uid: {subactor_uid}\n'
        f'remote task: {subactor_task_uid}\n'
    )
    DebugStatus.shield_sigint()
    try:
        Lock._blocked.add(req_task_uid)
        with (
            # NOTE: though a cs is created for every subactor lock
            # REQUEST in this ctx-child task, only the root-task
            # holding the `Lock` (on behalf of the ctx parent task
            # in a subactor) will set
            # `Lock._locking_task_cs` such that if the
            # lock holdingn task ever needs to be cancelled (since
            # it's shielded by default) that global ref can be
            # used to do so!
            trio.CancelScope(shield=True) as debug_lock_cs,

            # TODO: make this ONLY limit the pld_spec such that we
            # can on-error-decode-`.pld: Raw` fields in
            # `Context._deliver_msg()`?
            _codec.limit_msg_spec(
                payload_spec=__msg_spec__,
            ) as codec,
        ):
            # sanity?
            # TODO: don't need the ref right?
            assert codec is _codec.current_codec()

            async with _acquire_debug_lock_from_root_task(
                subactor_uid,
                subactor_task_uid,
            ):
                # XXX SUPER IMPORTANT BELOW IS ON THIS LINE XXX
                # without that the root cs might be,
                # - set and then removed in the finally block by 
                #   a task that never acquired the lock, leaving 
                # - the task that DID acquire the lock STUCK since
                #   it's original cs was GC-ed bc the first task
                #   already set the global ref to `None`
                Lock.set_locking_task_cs(debug_lock_cs)

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

    finally:
        debug_lock_cs.cancel()
        Lock._blocked.remove(req_task_uid)
        Lock.set_locking_task_cs(None)
        DebugStatus.unshield_sigint()


@cm
def apply_debug_codec() -> _codec.MsgCodec:
    '''
    Apply the subactor TTY `Lock`-ing protocol's msgspec temporarily
    (only in the current task).

    '''
    with (
        _codec.limit_msg_spec(
            payload_spec=__msg_spec__,
        ) as debug_codec,
    ):
        assert debug_codec is _codec.current_codec()
        log.pdb(
            'Applied `.devx._debug` msg-spec via codec\n'
            f'{debug_codec}\n'
        )
        yield debug_codec

    log.pdb(
        'REMOVED `.devx._debug` msg-spec via codec\n'
        f'{debug_codec}\n'
    )


async def wait_for_parent_stdin_hijack(
    actor_uid: tuple[str, str],
    task_uid: tuple[str, int],
    task_status: TaskStatus[trio.CancelScope] = trio.TASK_STATUS_IGNORED
):
    '''
    Connect to the root actor via a ``Context`` and invoke a task which
    locks a root-local TTY lock: ``lock_tty_for_child()``; this func
    should be called in a new task from a child actor **and never the
    root*.

    This function is used by any sub-actor to acquire mutex access to
    the ``pdb`` REPL and thus the root's TTY for interactive debugging
    (see below inside ``pause()``). It can be used to ensure that
    an intermediate nursery-owning actor does not clobber its children
    if they are in debug (see below inside
    ``maybe_wait_for_debugger()``).

    '''
    from .._discovery import get_root

    with (
        trio.CancelScope(shield=True) as cs,
        apply_debug_codec(),
    ):
        DebugStatus.req_cs = cs
        try:
            # TODO: merge into sync async with ?
            async with get_root() as portal:
                # this syncs to child's ``Context.started()`` call.
                async with portal.open_context(
                    lock_tty_for_child,
                    subactor_uid=actor_uid,
                    subactor_task_uid=task_uid,

                ) as (ctx, resp):
                    log.pdb(
                        'Subactor locked TTY with msg\n\n'
                        f'{resp}\n'
                    )
                    assert resp.subactor_uid == actor_uid
                    assert resp.cid

                    async with ctx.open_stream() as stream:
                        try:  # to unblock local caller
                            assert DebugStatus.repl_release
                            task_status.started(cs)

                            # wait for local task to exit and
                            # release the REPL
                            await DebugStatus.repl_release.wait()

                        finally:
                            await stream.send(
                                LockRelease(
                                    subactor_uid=actor_uid,
                                    cid=resp.cid,
                                )
                            )

                        # sync with callee termination
                        status: LockStatus = await ctx.result()
                        assert not status.locked

                log.pdb(
                    'TTY lock was released for subactor with msg\n\n'
                    f'{status}\n\n'
                    'Exitting {ctx.side!r} side locking of locking ctx'
                )

        except ContextCancelled:
            log.warning('Root actor cancelled debug lock')
            raise

        finally:
            DebugStatus.repl_task = None
            log.debug('Exiting debugger TTY lock request func from child')


    log.cancel('Reverting SIGINT handler!')
    DebugStatus.unshield_sigint()



def mk_mpdb() -> PdbREPL:
    '''
    Deliver a new `PdbREPL`: a multi-process safe `pdbp`
    REPL using the magic of SC!

    Our `pdb.Pdb` subtype accomplishes multi-process safe debugging
    by:

    - mutexing access to the root process' TTY & stdstreams
      via an IPC managed `Lock` singleton per process tree.

    - temporarily overriding any subactor's SIGINT handler to shield during
      live REPL sessions in sub-actors such that cancellation is
      never (mistakenly) triggered by a ctrl-c and instead only 
      by either explicit requests in the runtime or 

    '''
    pdb = PdbREPL()

    # Always shield out SIGINTs for subactors when REPL is active.
    #
    # XXX detect whether we're running from a non-main thread
    # in which case schedule the SIGINT shielding override
    # to in the main thread.
    # https://docs.python.org/3/library/signal.html#signals-and-threads
    DebugStatus.shield_sigint()

    # XXX: These are the important flags mentioned in
    # https://github.com/python-trio/trio/issues/1155
    # which resolve the traceback spews to console.
    pdb.allow_kbdint = True
    pdb.nosigint = True

    return pdb


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
    uid_in_debug: tuple[str, str]|None = Lock.global_actor_in_debug

    actor: Actor = current_actor()
    case_handled: bool = False

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

    # try to see if the supposed (sub)actor in debug still
    # has an active connection to *this* actor, and if not
    # it's likely they aren't using the TTY lock / debugger
    # and we should propagate SIGINT normally.
    any_connected: bool = False
    if uid_in_debug is not None:
        chans: list[tractor.Channel] = actor._peers.get(
            tuple(uid_in_debug)
        )
        if chans:
            any_connected = any(chan.connected() for chan in chans)
            if not any_connected:
                log.warning(
                    'A global actor reported to be in debug '
                    'but no connection exists for this child!?\n'
                    f'subactor_uid: {uid_in_debug}\n\n'
                    'Allowing SIGINT propagation..'
                )
                return do_cancel()

    # only set in the actor actually running the REPL
    repl: PdbREPL|None = Lock.repl

    # TODO: maybe we should flatten out all these cases using
    # a match/case?
    #
    # root actor branch that reports whether or not a child
    # has locked debugger.
    if is_root_process():
        lock_cs: trio.CancelScope = Lock.get_locking_task_cs()

        log.warning(
            f'root {actor.uid} handling SIGINT\n'
            f'any_connected: {any_connected}\n\n'

            f'{Lock.repr()}\n'
        )

        maybe_stale_lock_cs: bool = (
            lock_cs is not None
            # and not lock_cs.cancel_called
            and uid_in_debug is None
        )
        if maybe_stale_lock_cs:
            log.warning(
                'Stale `Lock._locking_task_cs: CancelScope` DETECTED?\n'
                f'|_{lock_cs}\n\n'
            )
            lock_cs.cancel()

        if uid_in_debug:  # "someone" is (ostensibly) using debug `Lock`
            name_in_debug: str = uid_in_debug[0]
            if (
                not repl  # but it's NOT us, the root actor.
            ):
                # sanity: since no repl ref is set, we def shouldn't
                # be the lock owner!
                assert name_in_debug != 'root'

                # XXX: only if there is an existing connection to the
                # (sub-)actor in debug do we ignore SIGINT in this
                # parent! Otherwise we may hang waiting for an actor
                # which has already terminated to unlock.
                if any_connected:  # there are subactors we can contact
                    # NOTE: don't emit this with `.pdb()` level in
                    # root without a higher level.
                    log.debug(
                        f'Ignoring SIGINT while debug REPL in use by child\n'
                        f'subactor: {uid_in_debug}\n'
                    )
                    # returns here minus tail logic
                    case_handled = True

                else:
                    message: str = (
                        f'Ignoring SIGINT while debug REPL SUPPOSEDLY in use by child\n'
                        f'subactor: {uid_in_debug}\n\n'
                        f'BUT, no child actors are contactable!?!?\n\n'

                        # f'Reverting to def `trio` SIGINT handler..\n'
                    )

                    if maybe_stale_lock_cs:
                        lock_cs.cancel()
                        message += (
                            'Maybe `Lock._locking_task_cs: CancelScope` is stale?\n'
                            f'|_{lock_cs}\n\n'
                        )

                    log.warning(message)
                    # Lock.unshield_sigint()
                    DebugStatus.unshield_sigint()
                    case_handled = True

            else:
                assert name_in_debug == 'root'  # we are the registered locker
                assert repl  # we have a pdb REPL engaged
                log.pdb(
                    f'Ignoring SIGINT while debug REPL in use\n'
                    f'root actor: {uid_in_debug}\n'
                )
                # returns here minus tail logic
                case_handled = True

        # root actor still has this SIGINT handler active without
        # an actor using the `Lock` (a bug state) ??
        # => so immediately cancel any stale lock cs and revert
        # the handler!
        else:
            # XXX revert back to ``trio`` handler since this handler shouldn't 
            # be enabled withtout an actor using a debug REPL!
            log.warning(
                'Ignoring SIGINT in root actor but no actor using a `pdb` REPL?\n'
                'Reverting SIGINT handler to `trio` default!\n'
            )

            if maybe_stale_lock_cs:
                lock_cs.cancel()

            DebugStatus.unshield_sigint()
            case_handled = True

    # child actor that has locked the debugger
    elif not is_root_process():
        log.warning(
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
                'A global sub-actor reported to be in debug '
                'but it has no connection to its parent ??\n'
                f'{uid_in_debug}\n'
                'Allowing SIGINT propagation..'
            )
            DebugStatus.unshield_sigint()
            # do_cancel()
            case_handled = True

        task: str|None = DebugStatus.repl_task
        if (
            task
            and
            repl
        ):
        # if repl:
            log.pdb(
                f'Ignoring SIGINT while local task using debug REPL\n'
                f'|_{task}\n'
                f'  |_{repl}\n'
            )
            case_handled = True
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
            case_handled = True

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
        repl  # only when this actor has a REPL engaged
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

    if not case_handled:
        log.critical(
            f'{actor.uid} UNHANDLED SIGINT !?!?\n'
            # TODO: pprint for `Lock`?
        )


_pause_msg: str = 'Attaching to pdb REPL in actor'


def _set_trace(
    actor: tractor.Actor|None = None,
    pdb: PdbREPL|None = None,
    shield: bool = False,

    extra_frames_up_when_async: int = 1,
    hide_tb: bool = True,
):
    __tracebackhide__: bool = hide_tb

    actor: tractor.Actor = (
        actor
        or
        current_actor()
    )

    # always start 1 level up from THIS in user code.
    frame: FrameType|None
    if frame := sys._getframe():
        frame: FrameType = frame.f_back  # type: ignore

    if (
        frame
        and (
            pdb
            and actor is not None
        )
    ):
        # TODO: maybe print the actor supervion tree up to the
        # root here? Bo

        log.pdb(
            f'{_pause_msg}\n'
            '|\n'
            # TODO: make an `Actor.__repr()__`
            f'|_ {current_task()} @ {actor.uid}\n'
        )
        # no f!#$&* idea, but when we're in async land
        # we need 2x frames up?
        for i in range(extra_frames_up_when_async):
            frame: FrameType = frame.f_back
            log.debug(
                f'Going up frame_{i}:\n|_{frame}\n'
            )

    # engage ze REPL
    # B~()
    pdb.set_trace(frame=frame)


async def _pause(

    debug_func: Callable = _set_trace,

    # NOTE: must be passed in the `.pause_from_sync()` case!
    pdb: PdbREPL|None = None,

    # TODO: allow caller to pause despite task cancellation,
    # exactly the same as wrapping with:
    # with CancelScope(shield=True):
    #     await pause()
    # => the REMAINING ISSUE is that the scope's .__exit__() frame
    # is always show in the debugger on entry.. and there seems to
    # be no way to override it?..
    #
    shield: bool = False,
    hide_tb: bool = True,
    extra_frames_up_when_async: int = 4,

    task_status: TaskStatus[trio.Event] = trio.TASK_STATUS_IGNORED

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

    # task_name: str = task.name

    if (
        not DebugStatus.repl_release
        or
        DebugStatus.repl_release.is_set()
    ):
        DebugStatus.repl_release = trio.Event()

    if debug_func is not None:
        debug_func = partial(debug_func)

    if pdb is None:
        pdb: PdbREPL = mk_mpdb()

    def _enter_repl_sync(
        debug_func: Callable,
    ) -> None:
        __tracebackhide__: bool = hide_tb
        try:
            # TODO: do we want to support using this **just** for the
            # locking / common code (prolly to help address #320)?
            #
            if debug_func is None:
                task_status.started(Lock)
            else:
                # block here one (at the appropriate frame *up*) where
                # ``breakpoint()`` was awaited and begin handling stdio.
                log.debug('Entering sync world of the `pdb` REPL..')
                try:
                    # log.critical(
                    #     f'stack len: {len(pdb.stack)}\n'
                    # )
                    debug_func(
                        actor,
                        pdb,
                        extra_frames_up_when_async=extra_frames_up_when_async,
                        shield=shield,
                    )
                except BaseException:
                    log.exception(
                        'Failed to invoke internal `debug_func = '
                        f'{debug_func.func.__name__}`\n'
                    )
                    raise

        except bdb.BdbQuit:
            Lock.release()
            raise

    try:
        if is_root_process():

            # we also wait in the root-parent for any child that
            # may have the tty locked prior
            # TODO: wait, what about multiple root tasks acquiring it though?
            if Lock.global_actor_in_debug == actor.uid:
                # re-entrant root process already has it: noop.
                log.warning(
                    f'{task.name}@{actor.uid} already has TTY lock\n'
                    f'ignoring..'
                )
                await trio.lowlevel.checkpoint()
                return

            # XXX: since we need to enter pdb synchronously below,
            # we have to release the lock manually from pdb completion
            # callbacks. Can't think of a nicer way then this atm.
            if Lock._debug_lock.locked():
                log.warning(
                    'attempting to shield-acquire active TTY lock'
                    f' owned by {Lock.global_actor_in_debug}'
                )

                # must shield here to avoid hitting a ``Cancelled`` and
                # a child getting stuck bc we clobbered the tty
                with trio.CancelScope(shield=True):
                    await Lock._debug_lock.acquire()
            else:
                # may be cancelled
                await Lock._debug_lock.acquire()

            Lock.global_actor_in_debug = actor.uid
            DebugStatus.repl_task = task
            DebugStatus.repl = Lock.repl = pdb

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

            # mark local actor as "in debug mode" to avoid recurrent
            # entries/requests to the root process
            DebugStatus.repl_task = task

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

            # NOTE: MUST it here bc multiple tasks are spawned by any
            # one sub-actor AND there will be a race between when the
            # root locking task delivers the `Started(pld=LockStatus)`
            # and when the REPL is actually entered here. SO ensure
            # the codec is set before either are run!
            #
            with (
                # _codec.limit_msg_spec(
                #     payload_spec=__msg_spec__,
                # ) as debug_codec,
                trio.CancelScope(shield=shield),
            ):
                # async with trio.open_nursery() as tn:
                #     tn.cancel_scope.shield = True
                try:
                    # cs: trio.CancelScope = await tn.start(
                    cs: trio.CancelScope = await actor._service_n.start(
                        wait_for_parent_stdin_hijack,
                        actor.uid,
                        (task.name, id(task)),
                    )
                    # our locker task should be the one in ctx
                    # with the root actor
                    assert DebugStatus.req_cs is cs

                    # XXX used by the SIGINT handler to check if
                    # THIS actor is in REPL interaction
                    Lock.repl = pdb

                except RuntimeError:
                    Lock.release()

                    if actor._cancel_called:
                        # service nursery won't be usable and we
                        # don't want to lock up the root either way since
                        # we're in (the midst of) cancellation.
                        return

                    raise

                # enter REPL

                try:
                    _enter_repl_sync(debug_func)
                finally:
                    DebugStatus.unshield_sigint()

    except BaseException:
        log.exception(
            'Failed to engage debugger via `_pause()` ??\n'
        )
        raise


# XXX: apparently we can't do this without showing this frame
# in the backtrace on first entry to the REPL? Seems like an odd
# behaviour that should have been fixed by now. This is also why
# we scrapped all the @cm approaches that were tried previously.
# finally:
#     __tracebackhide__ = True
#     # frame = sys._getframe()
#     # last_f = frame.f_back
#     # last_f.f_globals['__tracebackhide__'] = True
#     # signal.signal = pdbp.hideframe(signal.signal)


async def pause(

    debug_func: Callable|None = _set_trace,

    # TODO: allow caller to pause despite task cancellation,
    # exactly the same as wrapping with:
    # with CancelScope(shield=True):
    #     await pause()
    # => the REMAINING ISSUE is that the scope's .__exit__() frame
    # is always show in the debugger on entry.. and there seems to
    # be no way to override it?..
    #
    shield: bool = False,
    task_status: TaskStatus[trio.Event] = trio.TASK_STATUS_IGNORED,

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
    __tracebackhide__: bool = True

    with trio.CancelScope(
        shield=shield,
    ) as cs:

        # NOTE: so the caller can always manually cancel even
        # if shielded!
        task_status.started(cs)
        return await _pause(
            debug_func=debug_func,
            shield=shield,
            task_status=task_status,
            **_pause_kwargs
        )


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
    mdb: PdbREPL = mk_mpdb()

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
        actor=actor,
        pdb=mdb,
        hide_tb=hide_tb,
        extra_frames_up_when_async=1,

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
async def breakpoint(**kwargs):
    log.warning(
        '`tractor.breakpoint()` is deprecated!\n'
        'Please use `tractor.pause()` instead!\n'
    )
    __tracebackhide__: bool = True
    await pause(
        # extra_frames_up_when_async=6,
        **kwargs
    )


_crash_msg: str = (
    'Attaching to pdb REPL in crashed actor'
)


def _post_mortem(
    actor: tractor.Actor,
    pdb: PdbREPL,
    shield: bool = False,

    # only for compat with `._set_trace()`..
    extra_frames_up_when_async=1,

) -> None:
    '''
    Enter the ``pdbpp`` port mortem entrypoint using our custom
    debugger instance.

    '''
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

    # TODO: only replacing this to add the
    # `end=''` to the print XD
    # pdbp.xpm(Pdb=lambda: pdb)
    info = sys.exc_info()
    print(traceback.format_exc(), end='')
    pdbp.post_mortem(
        t=info[2],
        Pdb=lambda: pdb,
    )


post_mortem = partial(
    pause,
    debug_func=_post_mortem,
)


async def _maybe_enter_pm(err):
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
        log.debug("Actor crashed, entering debug mode")
        try:
            await post_mortem()
        finally:
            Lock.release()
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
        cs = await n.start(
            wait_for_parent_stdin_hijack,
            subactor_uid,
        )
        yield cs
        cs.cancel()


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
        in_debug: tuple[str, str]|None = Lock.global_actor_in_debug

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
                log.pdb(
                    msg
                    +
                    '\nRoot is waiting on tty lock to release..\n'
                )
                with trio.CancelScope(shield=True):
                    await Lock.no_remote_has_tty.wait()
                log.pdb(
                    f'Child subactor released debug lock\n'
                    f'|_{in_debug}\n'
                )

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
                with trio.CancelScope(shield=True):
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
