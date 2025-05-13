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

'''
Root-actor TTY mutex-locking machinery.

'''
from __future__ import annotations
import asyncio
from contextlib import (
    asynccontextmanager as acm,
)
import textwrap
import threading
import signal
from typing import (
    Any,
    AsyncIterator,
    Callable,
    TypeAlias,
    TYPE_CHECKING,
)
from types import (
    FrameType,
)

from msgspec import Struct
import pdbp
import sniffio
import trio
from trio import CancelScope
from trio.lowlevel import (
    current_task,
)
from trio import (
    TaskStatus,
)
import tractor
from tractor.to_asyncio import run_trio_task_in_future
from tractor.log import get_logger
from tractor._context import Context
from tractor import _state
from tractor._exceptions import (
    DebugRequestError,
    InternalError,
)
from tractor._state import (
    current_actor,
    is_root_process,
)

if TYPE_CHECKING:
    from trio.lowlevel import Task
    from threading import Thread
    from tractor.ipc import (
        IPCServer,
    )
    from tractor._runtime import (
        Actor,
    )
    from ._repl import (
        PdbREPL,
    )

log = get_logger(__name__)


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


# TODO: instantiate this only in root from factory
# so as to allow runtime errors from subactors.
class Lock:
    '''
    Actor-tree-global debug lock state, exists only in a root process.

    Mostly to avoid a lot of global declarations for now XD.

    '''
    @staticmethod
    def get_locking_task_cs() -> CancelScope|None:
        if not is_root_process():
            raise RuntimeError(
                '`Lock.locking_task_cs` is invalid in subactors!'
            )

        if ctx := Lock.ctx_in_debug:
            return ctx._scope

        return None

    # TODO: once we convert to singleton-per-actor-style
    # @property
    # def stats(cls) -> trio.LockStatistics:
    #     return cls._debug_lock.statistics()

    # @property
    # def owner(cls) -> Task:
    #     return cls._debug_lock.statistics().owner

    #     ROOT ONLY
    # ------ - -------
    # the root-actor-ONLY singletons for,
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

    _owned_by_root: bool = False
    _debug_lock: trio.StrictFIFOLock = trio.StrictFIFOLock()
    _blocked: set[
        tuple[str, str]  # `Actor.uid` for per actor
        |str  # Context.cid for per task
    ] = set()

    @classmethod
    def repr(cls) -> str:
        lock_stats: trio.LockStatistics = cls._debug_lock.statistics()
        req: trio.Event|None = cls.req_handler_finished
        fields: str = (
            f'|_ ._blocked: {cls._blocked}\n'
            f'|_ ._debug_lock: {cls._debug_lock}\n'
            f'  {lock_stats}\n\n'

            f'|_ .ctx_in_debug: {cls.ctx_in_debug}\n'
            f'|_ .req_handler_finished: {req}\n'
        )
        if req:
            req_stats: trio.EventStatistics = req.statistics()
            fields += f'  {req_stats}\n'

        body: str = textwrap.indent(
            fields,
            prefix=' ',
        )
        return (
            f'<{cls.__name__}(\n'
            f'{body}'
            ')>\n\n'
        )

    @classmethod
    # @pdbp.hideframe
    def release(
        cls,
        raise_on_thread: bool = True,

    ) -> bool:
        '''
        Release the actor-tree global TTY stdio lock (only) from the
        `trio.run()`-main-thread.

        '''
        we_released: bool = False
        ctx_in_debug: Context|None = cls.ctx_in_debug
        repl_task: Task|Thread|None = DebugStatus.repl_task
        try:
            if not DebugStatus.is_main_trio_thread():
                thread: threading.Thread = threading.current_thread()
                message: str = (
                    '`Lock.release()` can not be called from a non-main-`trio` thread!\n'
                    f'{thread}\n'
                )
                if raise_on_thread:
                    raise RuntimeError(message)

                log.devx(message)
                return False

            task: Task = current_task()
            message: str = (
                'TTY NOT RELEASED on behalf of caller\n'
                f'|_{task}\n'
            )

            # sanity check that if we're the root actor
            # the lock is marked as such.
            # note the pre-release value may be diff the the
            # post-release task.
            if repl_task is task:
                assert cls._owned_by_root
                message: str = (
                    'TTY lock held by root-actor on behalf of local task\n'
                    f'|_{repl_task}\n'
                )
            else:
                assert DebugStatus.repl_task is not task

            lock: trio.StrictFIFOLock = cls._debug_lock
            owner: Task = lock.statistics().owner
            if (
                lock.locked()
                and
                (owner is task)
                # ^-NOTE-^ if we do NOT ensure this, `trio` will
                # raise a RTE when a non-owner tries to releasee the
                # lock.
                #
                # Further we need to be extra pedantic about the
                # correct task, greenback-spawned-task and/or thread
                # being set to the `.repl_task` such that the above
                # condition matches and we actually release the lock.
                #
                # This is particular of note from `.pause_from_sync()`!
            ):
                cls._debug_lock.release()
                we_released: bool = True
                if repl_task:
                    message: str = (
                        'TTY released on behalf of root-actor-local REPL owner\n'
                        f'|_{repl_task}\n'
                    )
                else:
                    message: str = (
                        'TTY released by us on behalf of remote peer?\n'
                        f'{ctx_in_debug}\n'
                    )

        except RuntimeError as rte:
            log.exception(
                'Failed to release `Lock._debug_lock: trio.FIFOLock`?\n'
            )
            raise rte

        finally:
            # IFF there are no more requesting tasks queued up fire, the
            # "tty-unlocked" event thereby alerting any monitors of the lock that
            # we are now back in the "tty unlocked" state. This is basically
            # and edge triggered signal around an empty queue of sub-actor
            # tasks that may have tried to acquire the lock.
            lock_stats: trio.LockStatistics = cls._debug_lock.statistics()
            req_handler_finished: trio.Event|None = Lock.req_handler_finished
            if (
                not lock_stats.owner
                and
                req_handler_finished is None
            ):
                message += (
                    '-> No new task holds the TTY lock!\n\n'
                    f'{Lock.repr()}\n'
                )

            elif (
                req_handler_finished  # new IPC ctx debug request active
                and
                lock.locked()  # someone has the lock
            ):
                behalf_of_task = (
                    ctx_in_debug
                    or
                    repl_task
                )
                message += (
                    f'A non-caller task still owns this lock on behalf of\n'
                    f'{behalf_of_task}\n'
                    f'lock owner task: {lock_stats.owner}\n'
                )

            if (
                we_released
                and
                ctx_in_debug
            ):
                cls.ctx_in_debug = None  # unset

            # post-release value (should be diff then value above!)
            repl_task: Task|Thread|None = DebugStatus.repl_task
            if (
                cls._owned_by_root
                and
                we_released
            ):
                cls._owned_by_root = False

                if task is not repl_task:
                    message += (
                        'Lock released by root actor on behalf of bg thread\n'
                        f'|_{repl_task}\n'
                    )

            if message:
                log.devx(message)

        return we_released

    @classmethod
    @acm
    async def acquire_for_ctx(
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
            # `lock_stdio_for_peer()` caller is cancelled, this line should
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
            #
            # IF we received a cancel during the shielded lock entry of some
            # next-in-queue requesting task, then the resumption here will
            # result in that ``trio.Cancelled`` being raised to our caller
            # (likely from `lock_stdio_for_peer()` below)!  In
            # this case the ``finally:`` below should trigger and the
            # surrounding caller side context should cancel normally
            # relaying back to the caller.

            yield cls._debug_lock

        finally:
            message :str = 'Exiting `Lock.acquire_for_ctx()` on behalf of sub-actor\n'
            if we_acquired:
                cls.release()
                message += '-> TTY lock released by child\n'

            else:
                message += '-> TTY lock never acquired by child??\n'

            log.runtime(
                f'{message}\n'
                f'{ctx}'
            )


def get_lock() -> Lock:
    return Lock


@tractor.context(
    # enable the locking msgspec
    pld_spec=__pld_spec__,
)
async def lock_stdio_for_peer(
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
    lock_blocked: bool = False
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
        Lock._blocked.add(ctx.cid)
        lock_blocked = True
        root_task_name: str = current_task().name
        if tuple(subactor_uid) in Lock._blocked:
            log.warning(
                f'Subactor is blocked from acquiring debug lock..\n'
                f'subactor_uid: {subactor_uid}\n'
                f'remote task: {subactor_task_uid}\n'
            )
            ctx._enter_debugger_on_cancel: bool = False
            message: str = (
                f'Debug lock blocked for subactor\n\n'
                f'x)<= {subactor_uid}\n\n'

                f'Likely because the root actor already started shutdown and is '
                'closing IPC connections for this child!\n\n'
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

        # NOTE: we use the IPC ctx's cancel scope directly in order to
        # ensure that on any transport failure, or cancellation request
        # from the child we expect
        # `Context._maybe_cancel_and_set_remote_error()` to cancel this
        # scope despite the shielding we apply below.
        debug_lock_cs: CancelScope = ctx._scope

        async with Lock.acquire_for_ctx(ctx=ctx):
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
        fail_reason: str = (
            f'on behalf of peer\n\n'
            f'x)<=\n'
            f'   |_{subactor_task_uid!r}@{ctx.chan.uid!r}\n'
            f'\n'
            'Forcing `Lock.release()` due to acquire failure!\n\n'
            f'x)=>\n'
            f'   {ctx}'
        )
        if isinstance(req_err, trio.Cancelled):
            fail_reason = (
                'Cancelled during stdio-mutex request '
                +
                fail_reason
            )
        else:
            fail_reason = (
                'Failed to deliver stdio-mutex request '
                +
                fail_reason
            )

        log.exception(fail_reason)
        Lock.release()
        raise

    finally:
        if lock_blocked:
            Lock._blocked.remove(ctx.cid)

        # wakeup any waiters since the lock was (presumably)
        # released, possibly only temporarily.
        we_finished.set()
        DebugStatus.unshield_sigint()


class DebugStateError(InternalError):
    '''
    Something inconsistent or unexpected happend with a sub-actor's
    debug mutex request to the root actor.

    '''


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
    # XXX local ref to the `pdbp.Pbp` instance, ONLY set in the
    # actor-process that currently has activated a REPL i.e. it
    # should be `None` (unset) in any other actor-process that does
    # not yet have the `Lock` acquired via a root-actor debugger
    # request.
    repl: PdbREPL|None = None

    # TODO: yet again this looks like a task outcome where we need
    # to sync to the completion of one task (and get its result)
    # being used everywhere for syncing..
    # -[ ] see if we can get our proto oco task-mngr to work for
    #   this?
    repl_task: Task|None = None
    # repl_thread: Thread|None = None
    # ^TODO?

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
        `Task` cancellation) in subactors when a `pdb` REPL
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
        from ._sigint import (
            sigint_shield,
        )
        #
        # XXX detect whether we're running from a non-main thread
        # in which case schedule the SIGINT shielding override
        # to in the main thread.
        # https://docs.python.org/3/library/signal.html#signals-and-threads
        if (
            not cls.is_main_trio_thread()
            and
            not _state._runtime_vars.get(
                '_is_infected_aio',
                False,
            )
        ):
            cls._orig_sigint_handler: Callable = trio.from_thread.run_sync(
                signal.signal,
                signal.SIGINT,
                sigint_shield,
            )

        else:
            cls._orig_sigint_handler = signal.signal(
                signal.SIGINT,
                sigint_shield,
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
        # if not cls.is_main_trio_thread():
        if (
            not cls.is_main_trio_thread()
            and
            not _state._runtime_vars.get(
                '_is_infected_aio',
                False,
            )
            # not current_actor().is_infected_aio()
            # ^XXX, since for bg-thr case will always raise..
        ):
            trio.from_thread.run_sync(
                signal.signal,
                signal.SIGINT,
                cls._trio_handler,
            )
        else:
            trio_h: Callable = cls._trio_handler
            # XXX should never really happen XXX
            if not trio_h:
                from ._repl import mk_pdb
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
        try:
            async_lib: str = sniffio.current_async_library()
        except sniffio.AsyncLibraryNotFoundError:
            async_lib = None

        is_main_thread: bool = trio._util.is_main_thread()
        # ^TODO, since this is private, @oremanj says
        # we should just copy the impl for now..?
        if is_main_thread:
            thread_name: str = 'main'
        else:
            thread_name: str = threading.current_thread().name

        is_trio_main = (
            is_main_thread
            and
            (async_lib == 'trio')
        )

        report: str = f'Running thread: {thread_name!r}\n'
        if async_lib:
            report += (
                f'Current async-lib detected by `sniffio`: {async_lib}\n'
            )
        else:
            report += (
                'No async-lib detected (by `sniffio`) ??\n'
            )
        if not is_trio_main:
            log.warning(report)

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
    # @pdbp.hideframe
    def release(
        cls,
        cancel_req_task: bool = False,
    ):
        repl_release: trio.Event = cls.repl_release
        try:
            # sometimes the task might already be terminated in
            # which case this call will raise an RTE?
            # See below for reporting on that..
            if (
                repl_release is not None
                and
                not repl_release.is_set()
            ):
                if cls.is_main_trio_thread():
                    repl_release.set()

                elif (
                    _state._runtime_vars.get(
                        '_is_infected_aio',
                        False,
                    )
                    # ^XXX, again bc we need to not except
                    # but for bg-thread case it will always raise..
                    #
                    # TODO, is there a better api then using
                    # `err_on_no_runtime=False` in the below?
                    # current_actor().is_infected_aio()
                ):
                    async def _set_repl_release():
                        repl_release.set()

                    fute: asyncio.Future = run_trio_task_in_future(
                        _set_repl_release
                    )
                    if not fute.done():
                        log.warning('REPL release state unknown..?')

                else:
                    # XXX NOTE ONLY used for bg root-actor sync
                    # threads, see `.pause_from_sync()`.
                    trio.from_thread.run_sync(
                        repl_release.set
                    )

        except RuntimeError as rte:
            log.exception(
                f'Failed to release debug-request ??\n\n'
                f'{cls.repr()}\n'
            )
            # pdbp.set_trace()
            raise rte

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

            # XXX WARNING needs very special caughtion, and we should
            # prolly make a more explicit `@property` API?
            #
            # - if unset in root multi-threaded case can cause
            #   issues with detecting that some root thread is
            #   using a REPL,
            #
            # - what benefit is there to unsetting, it's always
            #   set again for the next task in some actor..
            #   only thing would be to avoid in the sigint-handler
            #   logging when we don't need to?
            cls.repl = None

            # maybe restore original sigint handler
            # XXX requires runtime check to avoid crash!
            if current_actor(err_on_no_runtime=False):
                cls.unshield_sigint()


# TODO: use the new `@lowlevel.singleton` for this!
def get_debug_req() -> DebugStatus|None:
    return DebugStatus


async def request_root_stdio_lock(
    actor_uid: tuple[str, str],
    task_uid: tuple[str, int],

    shield: bool = False,
    task_status: TaskStatus[CancelScope] = trio.TASK_STATUS_IGNORED,
):
    '''
    Connect to the root actor for this actor's process tree and
    RPC-invoke a task which acquires the std-streams global `Lock`:
    a process-tree-global mutex which prevents multiple actors from
    entering `PdbREPL.interaction()` at the same time such that the
    parent TTY's stdio is never "clobbered" by simultaneous
    reads/writes.

    The actual `Lock` singleton instance exists ONLY in the root
    actor's memory space and does nothing more then manage
    process-tree global state,
    namely a `._debug_lock: trio.FIFOLock`.

    The actual `PdbREPL` interaction/operation is completely isolated
    to each sub-actor (process) with the root's `Lock` providing the
    multi-process mutex-syncing mechanism to avoid parallel REPL
    usage within an actor tree.

    '''
    log.devx(
        'Initing stdio-lock request task with root actor'
    )
    # TODO: can we implement this mutex more generally as
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
    req_err: BaseException|None = None
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
            # log.devx(
            #     'Request cancel-scope is:\n\n'
            #     f'{pformat_cs(req_cs, var_name="req_cs")}\n\n'
            # )
            DebugStatus.req_cs = req_cs
            req_ctx: Context|None = None
            ctx_eg: BaseExceptionGroup|None = None
            try:
                # TODO: merge into single async with ?
                async with get_root() as portal:
                    async with portal.open_context(
                        lock_stdio_for_peer,
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
                        # pld_spec=__pld_spec__,

                    ) as (req_ctx, status):

                        DebugStatus.req_ctx = req_ctx
                        log.devx(
                            'Subactor locked TTY with msg\n\n'
                            f'{status}\n'
                        )

                        # try:
                        if (locker := status.subactor_uid) != actor_uid:
                            raise DebugStateError(
                                f'Root actor locked by another peer !?\n'
                                f'locker: {locker!r}\n'
                                f'actor_uid: {actor_uid}\n'
                            )
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

            except* (
                tractor.ContextCancelled,
                trio.Cancelled,
            ) as _taskc_eg:
                ctx_eg = _taskc_eg
                log.cancel(
                    'Debug lock request was CANCELLED?\n\n'
                    f'<=c) {req_ctx}\n'
                    # f'{pformat_cs(req_cs, var_name="req_cs")}\n\n'
                    # f'{pformat_cs(req_ctx._scope, var_name="req_ctx._scope")}\n\n'
                )
                raise

            except* (
                BaseException,
            ) as _ctx_eg:
                ctx_eg = _ctx_eg
                message: str = (
                    'Failed during debug request dialog with root actor?\n'
                )
                if (req_ctx := DebugStatus.req_ctx):
                    message += (
                        f'<=x)\n'
                        f' |_{req_ctx}\n'
                        f'Cancelling IPC ctx!\n'
                    )
                    try:
                        await req_ctx.cancel()
                    except trio.ClosedResourceError  as terr:
                        ctx_eg.add_note(
                            # f'Failed with {type(terr)!r} x)> `req_ctx.cancel()` '
                            f'Failed with `req_ctx.cancel()` <x) {type(terr)!r} '
                        )

                else:
                    message += 'Failed in `Portal.open_context()` call ??\n'

                log.exception(message)
                ctx_eg.add_note(message)
                raise ctx_eg

    except BaseException as _req_err:
        req_err = _req_err

        # XXX NOTE, since new `trio` enforces strict egs by default
        # we have to always handle the eg explicitly given the
        # `Portal.open_context()` call above (which implicitly opens
        # a nursery).
        match req_err:
            case BaseExceptionGroup():
                # for an eg of just one taskc, just unpack and raise
                # since we want to propagate a plane ol' `Cancelled`
                # up from the `.pause()` call.
                excs: list[BaseException] = req_err.exceptions
                if (
                    len(excs) == 1
                    and
                    type(exc := excs[0]) in (
                        tractor.ContextCancelled,
                        trio.Cancelled,
                    )
                ):
                    log.cancel(
                        'Debug lock request CANCELLED?\n'
                        f'{req_ctx}\n'
                    )
                    raise exc
            case (
                tractor.ContextCancelled(),
                trio.Cancelled(),
            ):
                log.cancel(
                    'Debug lock request CANCELLED?\n'
                    f'{req_ctx}\n'
                )
                raise exc

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
            'Failed during stdio-locking dialog from root actor\n\n'

            f'<=x)\n'
            f'  |_{DebugStatus.req_ctx}\n'
        ) from req_err

    finally:
        log.devx('Exiting debugger TTY lock request func from child')
        # signal request task exit
        DebugStatus.req_finished.set()
        DebugStatus.req_task = None


def any_connected_locker_child() -> bool:
    '''
    Predicate to determine if a reported child subactor in debug
    is actually connected.

    Useful to detect stale `Lock` requests after IPC failure.

    '''
    actor: Actor = current_actor()
    server: IPCServer = actor.ipc_server

    if not is_root_process():
        raise InternalError('This is a root-actor only API!')

    if (
        (ctx := Lock.ctx_in_debug)
        and
        (uid_in_debug := ctx.chan.uid)
    ):
        chans: list[tractor.Channel] = server._peers.get(
            tuple(uid_in_debug)
        )
        if chans:
            return any(
                chan.connected()
                for chan in chans
            )

    return False
