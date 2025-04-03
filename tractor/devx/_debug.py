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
import asyncio
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
    NoRuntime,
    is_multi_cancelled,
)
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
    from trio.lowlevel import Task
    from threading import Thread
    from tractor.ipc import Channel
    from tractor._runtime import (
        Actor,
    )

log = get_logger(__name__)

# TODO: refine the internal impl and APIs in this module!
#
# -[ ] rework `._pause()` and it's branch-cases for root vs.
#     subactor:
#  -[ ] `._pause_from_root()` + `_pause_from_subactor()`?
#  -[ ]  do the de-factor based on bg-thread usage in
#    `.pause_from_sync()` & `_pause_from_bg_root_thread()`.
#  -[ ] drop `debug_func == None` case which is confusing af..
#  -[ ]  factor out `_enter_repl_sync()` into a util func for calling
#    the `_set_trace()` / `_post_mortem()` APIs?
#
# -[ ] figure out if we need `acquire_debug_lock()` and/or re-implement
#    it as part of the `.pause_from_sync()` rework per above?
#
# -[ ] pair the `._pause_from_subactor()` impl with a "debug nursery"
#   that's dynamically allocated inside the `._rpc` task thus
#   avoiding the `._service_n.start()` usage for the IPC request?
#  -[ ] see the TODO inside `._rpc._errors_relayed_via_ipc()`
#
# -[ ] impl a `open_debug_request()` which encaps all
#   `request_root_stdio_lock()` task scheduling deats
#   + `DebugStatus` state mgmt; which should prolly be re-branded as
#   a `DebugRequest` type anyway AND with suppoort for bg-thread
#   (from root actor) usage?
#
# -[ ] handle the `xonsh` case for bg-root-threads in the SIGINT
#     handler!
#   -[ ] do we need to do the same for subactors?
#   -[ ] make the failing tests finally pass XD
#
# -[ ] simplify `maybe_wait_for_debugger()` to be a root-task only
#     API?
#   -[ ] currently it's implemented as that so might as well make it
#     formal?


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
            # NOTE: for subactors the stdio lock is released via the
            # allocated RPC locker task, so for root we have to do it
            # manually.
            if (
                is_root_process()
                and
                Lock._debug_lock.locked()
                and
                DebugStatus.is_main_trio_thread()
            ):
                # Lock.release(raise_on_thread=False)
                Lock.release()

            # XXX AFTER `Lock.release()` for root local repl usage
            DebugStatus.release()

    def set_quit(self):
        try:
            super().set_quit()
        finally:
            if (
                is_root_process()
                and
                Lock._debug_lock.locked()
                and
                DebugStatus.is_main_trio_thread()
            ):
                # Lock.release(raise_on_thread=False)
                Lock.release()

            # XXX after `Lock.release()` for root local repl usage
            DebugStatus.release()

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
        raise InternalError('This is a root-actor only API!')

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


_ctlc_ignore_header: str = (
    'Ignoring SIGINT while debug REPL in use'
)

def sigint_shield(
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
        # log.warning(
        log.devx(
            'Handling SIGINT in root actor\n'
            f'{Lock.repr()}'
            f'{DebugStatus.repr()}\n'
        )
        # try to see if the supposed (sub)actor in debug still
        # has an active connection to *this* actor, and if not
        # it's likely they aren't using the TTY lock / debugger
        # and we should propagate SIGINT normally.
        any_connected: bool = any_connected_locker_child()

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
                    _ctlc_ignore_header
                    +
                    f' by child '
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
            if not DebugStatus.repl:
                # TODO: WHEN should we revert back to ``trio``
                # handler if this one is stale?
                # -[ ] maybe after a counts work of ctl-c mashes?
                # -[ ] use a state var like `stale_handler: bool`?
                problem += (
                    'No subactor is using a `pdb` REPL according `Lock.ctx_in_debug`?\n'
                    'BUT, the root should be using it, WHY this handler ??\n\n'
                    'So either..\n'
                    '- some root-thread is using it but has no `.repl` set?, OR\n'
                    '- something else weird is going on outside the runtime!?\n'
                )
            else:
                # NOTE: since we emit this msg on ctl-c, we should
                # also always re-print the prompt the tail block!
                log.pdb(
                    _ctlc_ignore_header
                    +
                    f' by root actor..\n'
                    f'{DebugStatus.repl_task}\n'
                    f' |_{repl}\n'
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

        repl_task: str|None = DebugStatus.repl_task
        req_task: str|None = DebugStatus.req_task
        if (
            repl_task
            and
            repl
        ):
            log.pdb(
                _ctlc_ignore_header
                +
                f' by local task\n\n'
                f'{repl_task}\n'
                f' |_{repl}\n'
            )
        elif req_task:
            log.debug(
                _ctlc_ignore_header
                +
                f' by local request-task and either,\n'
                f'- someone else is already REPL-in and has the `Lock`, or\n'
                f'- some other local task already is replin?\n\n'
                f'{req_task}\n'
            )

        # TODO can we remove this now?
        # -[ ] does this path ever get hit any more?
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

    # maybe redraw/print last REPL output to console since
    # we want to alert the user that more input is expect since
    # nothing has been done dur to ignoring sigint.
    if (
        DebugStatus.repl  # only when current actor has a REPL engaged
    ):
        flush_status: str = (
            'Flushing stdout to ensure new prompt line!\n'
        )

        # XXX: yah, mega hack, but how else do we catch this madness XD
        if (
            repl.shname == 'xonsh'
        ):
            flush_status += (
                '-> ALSO re-flushing due to `xonsh`..\n'
            )
            repl.stdout.write(repl.prompt)

        # log.warning(
        log.devx(
            flush_status
        )
        repl.stdout.flush()

        # TODO: better console UX to match the current "mode":
        # -[ ] for example if in sticky mode where if there is output
        #   detected as written to the tty we redraw this part underneath
        #   and erase the past draw of this same bit above?
        # repl.sticky = True
        # repl._print_if_sticky()

        # also see these links for an approach from `ptk`:
        # https://github.com/goodboy/tractor/issues/130#issuecomment-663752040
        # https://github.com/prompt-toolkit/python-prompt-toolkit/blob/c2c6af8a0308f9e5d7c0e28cb8a02963fe0ce07a/prompt_toolkit/patch_stdout.py
    else:
        log.devx(
        # log.warning(
            'Not flushing stdout since not needed?\n'
            f'|_{repl}\n'
        )

    # XXX only for tracing this handler
    log.devx('exiting SIGINT')


_pause_msg: str = 'Opening a pdb REPL in paused actor'


_repl_fail_msg: str|None = (
    'Failed to REPl via `_pause()` '
)


async def _pause(

    debug_func: Callable|partial|None,

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
    hide_tb: bool = True,
    called_from_sync: bool = False,
    called_from_bg_thread: bool = False,
    task_status: TaskStatus[
        tuple[Task, PdbREPL],
        trio.Event
    ] = trio.TASK_STATUS_IGNORED,
    **debug_func_kwargs,

) -> tuple[Task, PdbREPL]|None:
    '''
    Inner impl for `pause()` to avoid the `trio.CancelScope.__exit__()`
    stack frame when not shielded (since apparently i can't figure out
    how to hide it using the normal mechanisms..)

    Hopefully we won't need this in the long run.

    '''
    __tracebackhide__: bool = hide_tb
    pause_err: BaseException|None = None
    actor: Actor = current_actor()
    try:
        task: Task = current_task()
    except RuntimeError as rte:
        # NOTE, 2 cases we might get here:
        #
        # - ACTUALLY not a `trio.lowlevel.Task` nor runtime caller,
        #  |_ error out as normal
        #
        # - an infected `asycio` actor calls it from an actual
        #   `asyncio.Task`
        #  |_ in this case we DO NOT want to RTE!
        __tracebackhide__: bool = False
        if actor.is_infected_aio():
            log.exception(
                'Failed to get current `trio`-task?'
            )
            raise RuntimeError(
                'An `asyncio` task should not be calling this!?'
            ) from rte
        else:
            task = asyncio.current_task()

    if debug_func is not None:
        debug_func = partial(debug_func)

    # XXX NOTE XXX set it here to avoid ctl-c from cancelling a debug
    # request from a subactor BEFORE the REPL is entered by that
    # process.
    if (
        not repl
        and
        debug_func
    ):
        repl: PdbREPL = mk_pdb()
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
        debug_func: partial[None],
    ) -> None:
        __tracebackhide__: bool = hide_tb
        debug_func_name: str = (
            debug_func.func.__name__ if debug_func else 'None'
        )

        # TODO: do we want to support using this **just** for the
        # locking / common code (prolly to help address #320)?
        task_status.started((task, repl))
        try:
            if debug_func:
                # block here one (at the appropriate frame *up*) where
                # ``breakpoint()`` was awaited and begin handling stdio.
                log.devx(
                    'Entering sync world of the `pdb` REPL for task..\n'
                    f'{repl}\n'
                    f'  |_{task}\n'
                 )

                # set local task on process-global state to avoid
                # recurrent entries/requests from the same
                # actor-local task.
                DebugStatus.repl_task = task
                if repl:
                    DebugStatus.repl = repl
                else:
                    log.error(
                        'No REPl instance set before entering `debug_func`?\n'
                        f'{debug_func}\n'
                    )

                # invoke the low-level REPL activation routine which itself
                # should call into a `Pdb.set_trace()` of some sort.
                debug_func(
                    repl=repl,
                    hide_tb=hide_tb,
                    **debug_func_kwargs,
                )

            # TODO: maybe invert this logic and instead
            # do `assert debug_func is None` when
            # `called_from_sync`?
            else:
                if (
                    called_from_sync
                    and
                    not DebugStatus.is_main_trio_thread()
                ):
                    assert called_from_bg_thread
                    assert DebugStatus.repl_task is not task

                return (task, repl)

        except trio.Cancelled:
            log.exception(
                'Cancelled during invoke of internal\n\n'
                f'`debug_func = {debug_func_name}`\n'
            )
            # XXX NOTE: DON'T release lock yet
            raise

        except BaseException:
            __tracebackhide__: bool = False
            log.exception(
                'Failed to invoke internal\n\n'
                f'`debug_func = {debug_func_name}`\n'
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
    repl_task: Thread|Task|None = DebugStatus.repl_task
    if (
        not DebugStatus.repl_release
        or
        DebugStatus.repl_release.is_set()
    ):
        log.devx(
            'Setting new `DebugStatus.repl_release: trio.Event` for requesting task\n'
            f'|_{task}\n'
        )
        DebugStatus.repl_release = trio.Event()
    else:
        log.devx(
            'Already an existing actor-local REPL user task\n'
            f'|_{repl_task}\n'
        )

    # ^-NOTE-^ this must be created BEFORE scheduling any subactor
    # debug-req task since it needs to wait on it just after
    # `.started()`-ing back its wrapping `.req_cs: CancelScope`.

    repl_err: BaseException|None = None
    try:
        if is_root_process():
            # we also wait in the root-parent for any child that
            # may have the tty locked prior
            # TODO: wait, what about multiple root tasks (with bg
            # threads) acquiring it though?
            ctx: Context|None = Lock.ctx_in_debug
            repl_task: Task|None = DebugStatus.repl_task
            if (
                ctx is None
                and
                repl_task is task
                # and
                # DebugStatus.repl
                # ^-NOTE-^ matches for multi-threaded case as well?
            ):
                # re-entrant root process already has it: noop.
                log.warning(
                    f'This root actor task is already within an active REPL session\n'
                    f'Ignoring this recurrent`tractor.pause()` entry\n\n'
                    f'|_{task}\n'
                    # TODO: use `._frame_stack` scanner to find the @api_frame
                )
                with trio.CancelScope(shield=shield):
                    await trio.lowlevel.checkpoint()
                return (repl, task)

            # elif repl_task:
            #     log.warning(
            #         f'This root actor has another task already in REPL\n'
            #         f'Waitin for the other task to complete..\n\n'
            #         f'|_{task}\n'
            #         # TODO: use `._frame_stack` scanner to find the @api_frame
            #     )
            #     with trio.CancelScope(shield=shield):
            #         await DebugStatus.repl_release.wait()
            #         await trio.sleep(0.1)

            # must shield here to avoid hitting a `Cancelled` and
            # a child getting stuck bc we clobbered the tty
            with trio.CancelScope(shield=shield):
                ctx_line = '`Lock` in this root actor task'
                acq_prefix: str = 'shield-' if shield else ''
                if (
                    Lock._debug_lock.locked()
                ):
                    if ctx:
                        ctx_line: str = (
                            'active `Lock` owned by ctx\n\n'
                            f'{ctx}'
                        )
                    elif Lock._owned_by_root:
                        ctx_line: str = (
                            'Already owned by root-task `Lock`\n\n'
                            f'repl_task: {DebugStatus.repl_task}\n'
                            f'repl: {DebugStatus.repl}\n'
                        )
                    else:
                        ctx_line: str = (
                            '**STALE `Lock`** held by unknown root/remote task '
                            'with no request ctx !?!?'
                        )

                log.devx(
                    f'attempting to {acq_prefix}acquire '
                    f'{ctx_line}'
                )
                await Lock._debug_lock.acquire()
                Lock._owned_by_root = True
                # else:

                # if (
                #     not called_from_bg_thread
                #     and not called_from_sync
                # ):
                #     log.devx(
                #         f'attempting to {acq_prefix}acquire '
                #         f'{ctx_line}'
                #     )

                    # XXX: since we need to enter pdb synchronously below,
                    # and we don't want to block the thread that starts
                    # stepping through the application thread, we later
                    # must `Lock._debug_lock.release()` manually from
                    # some `PdbREPL` completion callback(`.set_[continue/exit]()`).
                    #
                    # So, when `._pause()` is called from a (bg/non-trio)
                    # thread, special provisions are needed and we need
                    # to do the `.acquire()`/`.release()` calls from
                    # a common `trio.task` (due to internal impl of
                    # `FIFOLock`). Thus we do not acquire here and
                    # instead expect `.pause_from_sync()` to take care of
                    # this detail depending on the caller's (threading)
                    # usage.
                    #
                    # NOTE that this special case is ONLY required when
                    # using `.pause_from_sync()` from the root actor
                    # since OW a subactor will instead make an IPC
                    # request (in the branch below) to acquire the
                    # `Lock`-mutex and a common root-actor RPC task will
                    # take care of `._debug_lock` mgmt!

            # enter REPL from root, no TTY locking IPC ctx necessary
            # since we can acquire the `Lock._debug_lock` directly in
            # thread.
            return _enter_repl_sync(debug_func)

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
            return _enter_repl_sync(debug_func)

    # TODO: prolly factor this plus the similar block from
    # `_enter_repl_sync()` into a common @cm?
    except BaseException as _pause_err:
        pause_err: BaseException = _pause_err
        _repl_fail_report: str|None = _repl_fail_msg
        if isinstance(pause_err, bdb.BdbQuit):
            log.devx(
                'REPL for pdb was explicitly quit!\n'
            )
            _repl_fail_report = None

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

        elif isinstance(pause_err, trio.Cancelled):
            _repl_fail_report += (
                'You called `tractor.pause()` from an already cancelled scope!\n\n'
                'Consider `await tractor.pause(shield=True)` to make it work B)\n'
            )

        else:
            _repl_fail_report += f'on behalf of {repl_task} ??\n'

        if _repl_fail_report:
            log.exception(_repl_fail_report)

        if not actor.is_infected_aio():
            DebugStatus.release(cancel_req_task=True)

        # sanity checks for ^ on request/status teardown
        # assert DebugStatus.repl is None  # XXX no more bc bg thread cases?
        assert DebugStatus.repl_task is None

        # sanity, for when hackin on all this?
        if not isinstance(pause_err, trio.Cancelled):
            req_ctx: Context = DebugStatus.req_ctx
            # if req_ctx:
            #     # XXX, bc the child-task in root might cancel it?
            #     # assert req_ctx._scope.cancel_called
            #     assert req_ctx.maybe_error

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
            or
            pause_err
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
    task: Task|Thread|None = None,
):
    __tracebackhide__: bool = hide_tb
    actor: tractor.Actor = actor or current_actor()
    task: Task|Thread = task or current_task()

    # else:
    # TODO: maybe print the actor supervion tree up to the
    # root here? Bo
    log.pdb(
        f'{_pause_msg}\n'
        f'>(\n'
        f'|_{actor.uid}\n'
        f'  |_{task}\n' #  @ {actor.uid}\n'
        # f'|_{task}\n'
        # ^-TODO-^ more compact pformating?
        # -[ ] make an `Actor.__repr()__`
        # -[ ] should we use `log.pformat_task_uid()`?
    )
    # presuming the caller passed in the "api frame"
    # (the last frame before user code - like `.pause()`)
    # then we only step up one frame to where the user
    # called our API.
    caller_frame: FrameType = api_frame.f_back  # type: ignore

    # pretend this frame is the caller frame to show
    # the entire call-stack all the way down to here.
    if not hide_tb:
        caller_frame: FrameType = inspect.currentframe()

    # engage ze REPL
    # B~()
    repl.set_trace(frame=caller_frame)


# XXX TODO! XXX, ensure `pytest -s` doesn't just
# hang on this being called in a test.. XD
# -[ ] maybe something in our test suite or is there
#     some way we can detect output capture is enabled
#     from the process itself?
# |_ronny: ?
#
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


async def maybe_init_greenback(**kwargs) -> None|ModuleType:
    try:
        if mod := maybe_import_greenback(**kwargs):
            await mod.ensure_portal()
            log.devx(
                '`greenback` portal opened!\n'
                'Sync debug support activated!\n'
            )
            return mod
    except BaseException:
        log.exception('Failed to init `greenback`..')
        raise

    return None


async def _pause_from_bg_root_thread(
    behalf_of_thread: Thread,
    repl: PdbREPL,
    hide_tb: bool,
    task_status: TaskStatus[Task] = trio.TASK_STATUS_IGNORED,
    **_pause_kwargs,
):
    '''
    Acquire the `Lock._debug_lock` from a bg (only need for
    root-actor) non-`trio` thread (started via a call to
    `.to_thread.run_sync()` in some actor) by scheduling this func in
    the actor's service (TODO eventually a special debug_mode)
    nursery. This task acquires the lock then `.started()`s the
    `DebugStatus.repl_release: trio.Event` waits for the `PdbREPL` to
    set it, then terminates very much the same way as
    `request_root_stdio_lock()` uses an IPC `Context` from a subactor
    to do the same from a remote process.

    This task is normally only required to be scheduled for the
    special cases of a bg sync thread running in the root actor; see
    the only usage inside `.pause_from_sync()`.

    '''
    global Lock
    # TODO: unify this copied code with where it was
    # from in `maybe_wait_for_debugger()`
    # if (
    #     Lock.req_handler_finished is not None
    #     and not Lock.req_handler_finished.is_set()
    #     and (in_debug := Lock.ctx_in_debug)
    # ):
    #     log.devx(
    #         '\nRoot is waiting on tty lock to release from\n\n'
    #         # f'{caller_frame_info}\n'
    #     )
    #     with trio.CancelScope(shield=True):
    #         await Lock.req_handler_finished.wait()

    #     log.pdb(
    #         f'Subactor released debug lock\n'
    #         f'|_{in_debug}\n'
    #     )
    task: Task = current_task()

    # Manually acquire since otherwise on release we'll
    # get a RTE raised by `trio` due to ownership..
    log.devx(
        'Trying to acquire `Lock` on behalf of bg thread\n'
        f'|_{behalf_of_thread}\n'
    )

    # NOTE: this is already a task inside the main-`trio`-thread, so
    # we don't need to worry about calling it another time from the
    # bg thread on which who's behalf this task is operating.
    DebugStatus.shield_sigint()

    out = await _pause(
        debug_func=None,
        repl=repl,
        hide_tb=hide_tb,
        called_from_sync=True,
        called_from_bg_thread=True,
        **_pause_kwargs
    )
    DebugStatus.repl_task = behalf_of_thread

    lock: trio.FIFOLock = Lock._debug_lock
    stats: trio.LockStatistics= lock.statistics()
    assert stats.owner is task
    assert Lock._owned_by_root
    assert DebugStatus.repl_release

    # TODO: do we actually need this?
    # originally i was trying to solve wy this was
    # unblocking too soon in a thread but it was actually
    # that we weren't setting our own `repl_release` below..
    while stats.owner is not task:
        log.devx(
            'Trying to acquire `._debug_lock` from {stats.owner} for\n'
            f'|_{behalf_of_thread}\n'
        )
        await lock.acquire()
        break

    # XXX NOTE XXX super important dawg..
    # set our own event since the current one might
    # have already been overriden and then set when the
    # last REPL mutex holder exits their sesh!
    # => we do NOT want to override any existing one
    #   and we want to ensure we set our own ONLY AFTER we have
    #   acquired the `._debug_lock`
    repl_release = DebugStatus.repl_release = trio.Event()

    # unblock caller thread delivering this bg task
    log.devx(
        'Unblocking root-bg-thread since we acquired lock via `._pause()`\n'
        f'|_{behalf_of_thread}\n'
    )
    task_status.started(out)

    # wait for bg thread to exit REPL sesh.
    try:
        await repl_release.wait()
    finally:
        log.devx(
            'releasing lock from bg root thread task!\n'
            f'|_ {behalf_of_thread}\n'
        )
        Lock.release()


def pause_from_sync(
    hide_tb: bool = True,
    called_from_builtin: bool = False,
    api_frame: FrameType|None = None,

    allow_no_runtime: bool = False,

    # proxy to `._pause()`, for ex:
    # shield: bool = False,
    # api_frame: FrameType|None = None,
    **_pause_kwargs,

) -> None:
    '''
    Pause a `tractor` scheduled task or thread from sync (non-async
    function) code.

    When `greenback` is installed we remap python's builtin
    `breakpoint()` hook to this runtime-aware version which takes
    care of all bg-thread detection and appropriate synchronization
    with the root actor's `Lock` to avoid mult-thread/process REPL
    clobbering Bo

    '''
    __tracebackhide__: bool = hide_tb
    repl_owner: Task|Thread|None = None
    try:
        actor: tractor.Actor = current_actor(
            err_on_no_runtime=False,
        )
        if (
            not actor
            and
            not allow_no_runtime
        ):
            raise NoRuntime(
                'The actor runtime has not been opened?\n\n'
                '`tractor.pause_from_sync()` is not functional without a wrapping\n'
                '- `async with tractor.open_nursery()` or,\n'
                '- `async with tractor.open_root_actor()`\n\n'

                'If you are getting this from a builtin `breakpoint()` call\n'
                'it might mean the runtime was started then '
                'stopped prematurely?\n'
            )
        message: str = (
            f'{actor.uid} task called `tractor.pause_from_sync()`\n'
        )

        repl: PdbREPL = mk_pdb()

        # message += f'-> created local REPL {repl}\n'
        is_trio_thread: bool = DebugStatus.is_main_trio_thread()
        is_root: bool = is_root_process()
        is_infected_aio: bool = actor.is_infected_aio()
        thread: Thread = threading.current_thread()

        asyncio_task: asyncio.Task|None = None
        if is_infected_aio:
            asyncio_task = asyncio.current_task()

        # TODO: we could also check for a non-`.to_thread` context
        # using `trio.from_thread.check_cancelled()` (says
        # oremanj) wherein we get the following outputs:
        #
        # `RuntimeError`: non-`.to_thread` spawned thread
        # noop: non-cancelled `.to_thread`
        # `trio.Cancelled`: cancelled `.to_thread`

        # when called from a (bg) thread, run an async task in a new
        # thread which will call `._pause()` manually with special
        # handling for root-actor caller usage.
        if (
            not is_trio_thread
            and
            not asyncio_task
        ):
            # TODO: `threading.Lock()` this so we don't get races in
            # multi-thr cases where they're acquiring/releasing the
            # REPL and setting request/`Lock` state, etc..
            repl_owner: Thread = thread

            # TODO: make root-actor bg thread usage work!
            if is_root:
                message += (
                    f'-> called from a root-actor bg {thread}\n'
                )

                message += (
                    '-> scheduling `._pause_from_bg_root_thread()`..\n'
                )
                # XXX SUBTLE BADNESS XXX that should really change!
                # don't over-write the `repl` here since when
                # this behalf-of-bg_thread-task calls pause it will
                # pass `debug_func=None` which will result in it
                # returing a `repl==None` output and that get's also
                # `.started(out)` back here! So instead just ignore
                # that output and assign the `repl` created above!
                bg_task, _ = trio.from_thread.run(
                    afn=partial(
                        actor._service_n.start,
                        partial(
                            _pause_from_bg_root_thread,
                            behalf_of_thread=thread,
                            repl=repl,
                            hide_tb=hide_tb,
                            **_pause_kwargs,
                        ),
                    ),
                )
                DebugStatus.shield_sigint()
                message += (
                    f'-> `._pause_from_bg_root_thread()` started bg task {bg_task}\n'
                )
            else:
                message += f'-> called from a bg {thread}\n'
                # NOTE: since this is a subactor, `._pause()` will
                # internally issue a debug request via
                # `request_root_stdio_lock()` and we don't need to
                # worry about all the special considerations as with
                # the root-actor per above.
                bg_task, _ = trio.from_thread.run(
                    afn=partial(
                        _pause,
                        debug_func=None,
                        repl=repl,
                        hide_tb=hide_tb,

                        # XXX to prevent `._pause()` for setting
                        # `DebugStatus.repl_task` to the gb task!
                        called_from_sync=True,
                        called_from_bg_thread=True,

                        **_pause_kwargs
                    ),
                )
                # ?TODO? XXX where do we NEED to call this in the
                # subactor-bg-thread case?
                DebugStatus.shield_sigint()
                assert bg_task is not DebugStatus.repl_task

        # TODO: once supported, remove this AND the one
        # inside `._pause()`!
        # outstanding impl fixes:
        # -[ ] need to make `.shield_sigint()` below work here!
        # -[ ] how to handle `asyncio`'s new SIGINT-handler
        #     injection?
        # -[ ] should `breakpoint()` work and what does it normally
        #     do in `asyncio` ctxs?
        # if actor.is_infected_aio():
        #     raise RuntimeError(
        #         '`tractor.pause[_from_sync]()` not yet supported '
        #         'for infected `asyncio` mode!'
        #     )
        elif (
            not is_trio_thread
            and
            is_infected_aio  # as in, the special actor-runtime mode
            # ^NOTE XXX, that doesn't mean the caller is necessarily
            # an `asyncio.Task` just that `trio` has been embedded on
            # the `asyncio` event loop!
            and
            asyncio_task  # transitive caller is an actual `asyncio.Task`
        ):
            greenback: ModuleType = maybe_import_greenback()

            if greenback.has_portal():
                DebugStatus.shield_sigint()
                fute: asyncio.Future = run_trio_task_in_future(
                    partial(
                        _pause,
                        debug_func=None,
                        repl=repl,
                        hide_tb=hide_tb,

                        # XXX to prevent `._pause()` for setting
                        # `DebugStatus.repl_task` to the gb task!
                        called_from_sync=True,
                        called_from_bg_thread=True,

                        **_pause_kwargs
                    )
                )
                repl_owner = asyncio_task
                bg_task, _ = greenback.await_(fute)
                # TODO: ASYNC version -> `.pause_from_aio()`?
                # bg_task, _ = await fute

            # handle the case where an `asyncio` task has been
            # spawned WITHOUT enabling a `greenback` portal..
            # => can often happen in 3rd party libs.
            else:
                bg_task = repl_owner

                # TODO, ostensibly we can just acquire the
                # debug lock directly presuming we're the
                # root actor running in infected asyncio
                # mode?
                #
                # TODO, this would be a special case where
                # a `_pause_from_root()` would come in very
                # handy!
                # if is_root:
                #     import pdbp; pdbp.set_trace()
                #     log.warning(
                #         'Allowing `asyncio` task to acquire debug-lock in root-actor..\n'
                #         'This is not fully implemented yet; there may be teardown hangs!\n\n'
                #     )
                # else:

                # simply unsupported, since there exists no hack (i
                # can think of) to workaround this in a subactor
                # which needs to lock the root's REPL ow we're sure
                # to get prompt stdstreams clobbering..
                cf_repr: str = ''
                if api_frame:
                    caller_frame: FrameType = api_frame.f_back
                    cf_repr: str = f'caller_frame: {caller_frame!r}\n'

                raise RuntimeError(
                    f"CAN'T USE `greenback._await()` without a portal !?\n\n"
                    f'Likely this task was NOT spawned via the `tractor.to_asyncio` API..\n'
                    f'{asyncio_task}\n'
                    f'{cf_repr}\n'

                    f'Prolly the task was started out-of-band (from some lib?)\n'
                    f'AND one of the below was never called ??\n'
                    f'- greenback.ensure_portal()\n'
                    f'- greenback.bestow_portal(<task>)\n'
                )

        else:  # we are presumably the `trio.run()` + main thread
            # raises on not-found by default
            greenback: ModuleType = maybe_import_greenback()

            # TODO: how to ensure this is either dynamically (if
            # needed) called here (in some bg tn??) or that the
            # subactor always already called it?
            # greenback: ModuleType = await maybe_init_greenback()

            message += f'-> imported {greenback}\n'

            # NOTE XXX seems to need to be set BEFORE the `_pause()`
            # invoke using gb below?
            DebugStatus.shield_sigint()
            repl_owner: Task = current_task()

            message += '-> calling `greenback.await_(_pause(debug_func=None))` from sync caller..\n'
            try:
                out = greenback.await_(
                    _pause(
                        debug_func=None,
                        repl=repl,
                        hide_tb=hide_tb,
                        called_from_sync=True,
                        **_pause_kwargs,
                    )
                )
            except RuntimeError as rte:
                if not _state._runtime_vars.get(
                        'use_greenback',
                        False,
                ):
                    raise RuntimeError(
                        '`greenback` was never initialized in this actor!?\n\n'
                        f'{_state._runtime_vars}\n'
                    ) from rte

                raise

            if out:
                bg_task, _ = out
            else:
                bg_task: Task = current_task()

            # assert repl is repl
            # assert bg_task is repl_owner
            if bg_task is not repl_owner:
                raise DebugStateError(
                    f'The registered bg task for this debug request is NOT its owner ??\n'
                    f'bg_task: {bg_task}\n'
                    f'repl_owner: {repl_owner}\n\n'

                    f'{DebugStatus.repr()}\n'
                )

        # NOTE: normally set inside `_enter_repl_sync()`
        DebugStatus.repl_task: str = repl_owner

        # TODO: ensure we aggressively make the user aware about
        # entering the global `breakpoint()` built-in from sync
        # code?
        message += (
            f'-> successfully scheduled `._pause()` in `trio` thread on behalf of {bg_task}\n'
            f'-> Entering REPL via `tractor._set_trace()` from caller {repl_owner}\n'
        )
        log.devx(message)

        # NOTE set as late as possible to avoid state clobbering
        # in the multi-threaded case!
        DebugStatus.repl = repl

        _set_trace(
            api_frame=api_frame or inspect.currentframe(),
            repl=repl,
            hide_tb=hide_tb,
            actor=actor,
            task=repl_owner,
        )
        # LEGACY NOTE on next LOC's frame showing weirdness..
        #
        # XXX NOTE XXX no other LOC can be here without it
        # showing up in the REPL's last stack frame !?!
        # -[ ] tried to use `@pdbp.hideframe` decoration but
        #   still doesn't work
    except BaseException as err:
        log.exception(
            'Failed to sync-pause from\n\n'
            f'{repl_owner}\n'
        )
        __tracebackhide__: bool = False
        raise err


def _sync_pause_from_builtin(
    *args,
    called_from_builtin=True,
    **kwargs,
) -> None:
    '''
    Proxy call `.pause_from_sync()` but indicate the caller is the
    `breakpoint()` built-in.

    Note: this assigned to `os.environ['PYTHONBREAKPOINT']` inside `._root`

    '''
    pause_from_sync(
        *args,
        called_from_builtin=True,
        api_frame=inspect.currentframe(),
        **kwargs,
    )


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
    'Opening a pdb REPL in crashed actor'
)


def _post_mortem(
    repl: PdbREPL,  # normally passed by `_pause()`

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
    try:
        actor: tractor.Actor = current_actor()
        actor_repr: str = str(actor.uid)
        # ^TODO, instead a nice runtime-info + maddr + uid?
        # -[ ] impl a `Actor.__repr()__`??
        #  |_ <task>:<thread> @ <actor>
        # no_runtime: bool = False

    except NoRuntime:
        actor_repr: str = '<no-actor-runtime?>'
        # no_runtime: bool = True

    try:
        task_repr: Task = current_task()
    except RuntimeError:
        task_repr: str = '<unknown-Task>'

    # TODO: print the actor supervion tree up to the root
    # here! Bo
    log.pdb(
        f'{_crash_msg}\n'
        f'x>(\n'
        f' |_ {task_repr} @ {actor_repr}\n'

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
    # if not no_runtime:
    #     DebugStatus.release()
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

    # only enter debugger REPL when returns `True`
    debug_filter: Callable[
        [BaseException|BaseExceptionGroup],
        bool,
    ] = lambda err: not is_multi_cancelled(err),

):
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
        and
        debug_filter(err)
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
    Request to acquire the TTY `Lock` in the root actor, release on
    exit.

    This helper is for actor's who don't actually need to acquired
    the debugger but want to wait until the lock is free in the
    process-tree root such that they don't clobber an ongoing pdb
    REPL session in some peer or child!

    '''
    if not debug_mode():
        yield None
        return

    task: Task = current_task()
    async with trio.open_nursery() as n:
        ctx: Context = await n.start(
            partial(
                request_root_stdio_lock,
                actor_uid=subactor_uid,
                task_uid=(task.name, id(task)),
            )
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
        and
        not child_in_debug
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
                    '\n^^ Root is waiting on tty lock release.. ^^\n'
                    # f'{caller_frame_info}\n'
                )

                if not any_connected_locker_child():
                    Lock.get_locking_task_cs().cancel()

                with trio.CancelScope(shield=True):
                    await Lock.req_handler_finished.wait()

                log.devx(
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

        return True

    # else:
    #     # TODO: non-root call for #320?
    #     this_uid: tuple[str, str] = current_actor().uid
    #     async with acquire_debug_lock(
    #         subactor_uid=this_uid,
    #     ):
    #         pass
    return False


class BoxedMaybeException(Struct):
    '''
    Box a maybe-exception for post-crash introspection usage
    from the body of a `open_crash_handler()` scope.

    '''
    value: BaseException|None = None


# TODO: better naming and what additionals?
# - [ ] optional runtime plugging?
# - [ ] detection for sync vs. async code?
# - [ ] specialized REPL entry when in distributed mode?
# -[x] hide tb by def
# - [x] allow ignoring kbi Bo
@cm
def open_crash_handler(
    catch: set[BaseException] = {
        BaseException,
    },
    ignore: set[BaseException] = {
        KeyboardInterrupt,
        trio.Cancelled,
    },
    tb_hide: bool = True,
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
    __tracebackhide__: bool = tb_hide

    # TODO, yield a `outcome.Error`-like boxed type?
    # -[~] use `outcome.Value/Error` X-> frozen!
    # -[x] write our own..?
    # -[ ] consider just wtv is used by `pytest.raises()`?
    #
    boxed_maybe_exc = BoxedMaybeException()
    err: BaseException
    try:
        yield boxed_maybe_exc
    except tuple(catch) as err:
        boxed_maybe_exc.value = err
        if (
            type(err) not in ignore
            and
            not is_multi_cancelled(
                err,
                ignore_nested=ignore
            )
        ):
            try:
                # use our re-impl-ed version
                _post_mortem(
                    repl=mk_pdb(),
                    tb=sys.exc_info()[2],
                    api_frame=inspect.currentframe().f_back,
                )
            except bdb.BdbQuit:
                __tracebackhide__: bool = False
                raise err

            # XXX NOTE, `pdbp`'s version seems to lose the up-stack
            # tb-info?
            # pdbp.xpm()

        raise err


@cm
def maybe_open_crash_handler(
    pdb: bool = False,
    tb_hide: bool = True,

    **kwargs,
):
    '''
    Same as `open_crash_handler()` but with bool input flag
    to allow conditional handling.

    Normally this is used with CLI endpoints such that if the --pdb
    flag is passed the pdb REPL is engaed on any crashes B)
    '''
    __tracebackhide__: bool = tb_hide

    rtctx = nullcontext(
        enter_result=BoxedMaybeException()
    )
    if pdb:
        rtctx = open_crash_handler(**kwargs)

    with rtctx as boxed_maybe_exc:
        yield boxed_maybe_exc
