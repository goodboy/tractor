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
Debugger/tracing public API.

Essentially providing the same
`pdb(p).set_trace()`/`breakpoint()`-style REPL UX but with seemless
mult-process support within a single actor tree.

'''
from __future__ import annotations
import asyncio
import bdb
from contextlib import (
    AbstractContextManager,
)
from functools import (
    partial,
)
import inspect
import threading
from typing import (
    Callable,
    TYPE_CHECKING,
)
from types import (
    FrameType,
    ModuleType,
)

import trio
from trio.lowlevel import (
    current_task,
    Task,
)
from trio import (
    TaskStatus,
)
import tractor
from tractor.log import get_logger
from tractor.to_asyncio import run_trio_task_in_future
from tractor._context import Context
from tractor import _state
from tractor._exceptions import (
    NoRuntime,
)
from tractor._state import (
    current_actor,
    current_ipc_ctx,
    is_root_process,
)
from ._repl import (
    PdbREPL,
    mk_pdb,
    TractorConfig as TractorConfig,
)
from ._tty_lock import (
    DebugStatus,
    DebugStateError,
    Lock,
    request_root_stdio_lock,
)
from ._sigint import (
    sigint_shield as sigint_shield,
    _ctlc_ignore_header as _ctlc_ignore_header
)

if TYPE_CHECKING:
    from trio.lowlevel import Task
    from threading import Thread
    from tractor._runtime import (
        Actor,
    )
    # from ._post_mortem import BoxedMaybeException
    from ._repl import PdbREPL

log = get_logger(__package__)

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

    # maybe pre/post REPL entry
    repl_fixture: (
        AbstractContextManager[bool]
        |None
    ) = None,

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

        # maybe enter any user fixture
        enter_repl: bool = DebugStatus.maybe_enter_repl_fixture(
            repl=repl,
            repl_fixture=repl_fixture,
        )
        if not enter_repl:
            return

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

    log.debug(
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
        log.debug(
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

                log.debug(
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

        # CASE: bg-thread spawned via `trio.to_thread`
        # -----
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
        #
        # CASE: bg-thread running `asyncio.Task`
        # -----
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

        # CASE: `trio.run()` + "main thread"
        # -----
        else:
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

    Note: this always assigned to `os.environ['PYTHONBREAKPOINT']`
    inside `._root.open_root_actor()` whenever `debug_mode=True` is
    set.

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
