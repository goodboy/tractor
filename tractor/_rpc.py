# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
Remote (task) Procedure Call (scheduling) with SC transitive semantics.

'''
from __future__ import annotations
from contextlib import (
    asynccontextmanager as acm,
    aclosing,
)
from functools import partial
import inspect
from pprint import pformat
from typing import (
    Any,
    Callable,
    Coroutine,
    TYPE_CHECKING,
)
import warnings

import trio
from trio import (
    CancelScope,
    Nursery,
    TaskStatus,
)

from ._ipc import Channel
from ._context import (
    Context,
)
from ._exceptions import (
    ContextCancelled,
    ModuleNotExposed,
    MsgTypeError,
    TransportClosed,
    is_multi_cancelled,
    pack_error,
    unpack_error,
)
from .devx import (
    _debug,
    add_div,
)
from . import _state
from .log import get_logger
from .msg import (
    current_codec,
    MsgCodec,
    NamespacePath,
    pretty_struct,
)
from tractor.msg.types import (
    CancelAck,
    Error,
    Msg,
    MsgType,
    Return,
    Start,
    StartAck,
    Started,
    Stop,
    Yield,
)

if TYPE_CHECKING:
    from ._runtime import Actor

log = get_logger('tractor')


async def _invoke_non_context(
    actor: Actor,
    cancel_scope: CancelScope,
    ctx: Context,
    cid: str,
    chan: Channel,
    func: Callable,
    coro: Coroutine,
    kwargs: dict[str, Any],

    treat_as_gen: bool,
    is_rpc: bool,
    return_msg: Return|CancelAck = Return,

    task_status: TaskStatus[
        Context | BaseException
    ] = trio.TASK_STATUS_IGNORED,
):
    __tracebackhide__: bool = True

    # TODO: can we unify this with the `context=True` impl below?
    if inspect.isasyncgen(coro):
        await chan.send(
            StartAck(
                cid=cid,
                functype='asyncgen',
            )
        )
        # XXX: massive gotcha! If the containing scope
        # is cancelled and we execute the below line,
        # any ``ActorNursery.__aexit__()`` WON'T be
        # triggered in the underlying async gen! So we
        # have to properly handle the closing (aclosing)
        # of the async gen in order to be sure the cancel
        # is propagated!
        with cancel_scope as cs:
            ctx._scope = cs
            task_status.started(ctx)
            async with aclosing(coro) as agen:
                async for item in agen:
                    # TODO: can we send values back in here?
                    # it's gonna require a `while True:` and
                    # some non-blocking way to retrieve new `asend()`
                    # values from the channel:
                    # to_send = await chan.recv_nowait()
                    # if to_send is not None:
                    #     to_yield = await coro.asend(to_send)
                    await chan.send(
                        Yield(
                            cid=cid,
                            pld=item,
                        )
                    )

        log.runtime(f"Finished iterating {coro}")
        # TODO: we should really support a proper
        # `StopAsyncIteration` system here for returning a final
        # value if desired
        await chan.send(
            Stop(cid=cid)
        )

    # one way @stream func that gets treated like an async gen
    # TODO: can we unify this with the `context=True` impl below?
    elif treat_as_gen:
        await chan.send(
            StartAck(
                cid=cid,
                functype='asyncgen',
            )
        )
        # XXX: the async-func may spawn further tasks which push
        # back values like an async-generator would but must
        # manualy construct the response dict-packet-responses as
        # above
        with cancel_scope as cs:
            ctx._scope = cs
            task_status.started(ctx)
            await coro

        if not cs.cancelled_caught:
            # task was not cancelled so we can instruct the
            # far end async gen to tear down
            await chan.send(
                Stop(cid=cid)
            )
    else:
        # regular async function/method
        # XXX: possibly just a scheduled `Actor._cancel_task()`
        # from a remote request to cancel some `Context`.
        # ------ - ------
        # TODO: ideally we unify this with the above `context=True`
        # block such that for any remote invocation ftype, we
        # always invoke the far end RPC task scheduling the same
        # way: using the linked IPC context machinery.
        failed_resp: bool = False
        try:
            ack = StartAck(
                cid=cid,
                functype='asyncfunc',
            )
            await chan.send(ack)
        except (
            trio.ClosedResourceError,
            trio.BrokenResourceError,
            BrokenPipeError,
        ) as ipc_err:
            failed_resp = True
            if is_rpc:
                raise ipc_err
            else:
                log.exception(
                    f'Failed to respond to runtime RPC request for\n\n'
                    f'{ack}\n'
                )

        with cancel_scope as cs:
            ctx._scope: CancelScope = cs
            task_status.started(ctx)
            result = await coro
            fname: str = func.__name__
            log.runtime(
                'RPC complete:\n'
                f'task: {ctx._task}\n'
                f'|_cid={ctx.cid}\n'
                f'|_{fname}() -> {pformat(result)}\n'
            )

            # NOTE: only send result if we know IPC isn't down
            if (
                not failed_resp
                and chan.connected()
            ):
                try:
                    ret_msg = return_msg(
                        cid=cid,
                        pld=result,
                    )
                    await chan.send(ret_msg)
                except (
                    BrokenPipeError,
                    trio.BrokenResourceError,
                ):
                    log.warning(
                        'Failed to send RPC result?\n'
                        f'|_{func}@{actor.uid}() -> {ret_msg}\n\n'
                        f'x=> peer: {chan.uid}\n'
                    )

@acm
async def _errors_relayed_via_ipc(
    actor: Actor,
    chan: Channel,
    ctx: Context,
    is_rpc: bool,

    hide_tb: bool = False,
    debug_kbis: bool = False,
    task_status: TaskStatus[
        Context | BaseException
    ] = trio.TASK_STATUS_IGNORED,

) -> None:
    __tracebackhide__: bool = hide_tb
    # TODO: a debug nursery when in debug mode!
    # async with maybe_open_debugger_nursery() as debug_tn:
    # => see matching comment in side `._debug._pause()`
    try:
        yield  # run RPC invoke body

    # box and ship RPC errors for wire-transit via
    # the task's requesting parent IPC-channel.
    except (
        Exception,
        BaseExceptionGroup,
        KeyboardInterrupt,
    ) as err:

        # NOTE: always hide this frame from debug REPL call stack
        # if the crash originated from an RPC task and we DID NOT
        # fail due to an IPC transport error!
        if (
            is_rpc
            and
            chan.connected()
        ):
            __tracebackhide__: bool = hide_tb

        # TODO: maybe we'll want different "levels" of debugging
        # eventualy such as ('app', 'supervisory', 'runtime') ?
        #
        # -[ ] this if check is duplicate with `._maybe_enter_pm()`..
        if not is_multi_cancelled(err):
            entered_debug: bool = False
            if (
                (
                    not isinstance(err, ContextCancelled)
                    or (
                        isinstance(err, ContextCancelled)
                        and ctx._cancel_called

                        # if the root blocks the debugger lock request from a child
                        # we will get a remote-cancelled condition.
                        and ctx._enter_debugger_on_cancel
                    )
                )
                and
                (
                    not isinstance(err, KeyboardInterrupt)
                    or (
                        isinstance(err, KeyboardInterrupt)
                        and debug_kbis
                    )
                )
            ):
                # XXX QUESTION XXX: is there any case where we'll
                # want to debug IPC disconnects as a default?
                # => I can't think of a reason that inspecting this
                # type of failure will be useful for respawns or
                # recovery logic - the only case is some kind of
                # strange bug in our transport layer itself? Going
                # to keep this open ended for now.
                log.debug(
                    'RPC task crashed, attempting to enter debugger\n'
                    f'|_{ctx}'
                )
                entered_debug = await _debug._maybe_enter_pm(
                    err,
                    api_frame=inspect.currentframe(),
                )
                if not entered_debug:
                    log.exception(
                        'RPC task crashed\n'
                        f'|_{ctx}'
                    )

        # ALWAYS try to ship RPC errors back to parent/caller task
        if is_rpc:

            # TODO: tests for this scenario:
            # - RPC caller closes connection before getting a response
            #   should **not** crash this actor..
            await try_ship_error_to_remote(
                chan,
                err,
                cid=ctx.cid,
                remote_descr='caller',
                hide_tb=hide_tb,
            )

        # if the ctx cs is NOT allocated, the error is likely from
        # above `coro` invocation machinery NOT from inside the
        # `coro` itself, i.e. err is NOT a user application error.
        if ctx._scope is None:
            # we don't ever raise directly here to allow the
            # msg-loop-scheduler to continue running for this
            # channel.
            task_status.started(err)

        # always reraise KBIs so they propagate at the sys-process level.
        if isinstance(err, KeyboardInterrupt):
            raise

    # RPC task bookeeping.
    # since RPC tasks are scheduled inside a flat
    # `Actor._service_n`, we add "handles" to each such that
    # they can be individually ccancelled.
    finally:
        try:
            ctx: Context
            func: Callable
            is_complete: trio.Event
            (
                ctx,
                func,
                is_complete,
            ) = actor._rpc_tasks.pop(
                (chan, ctx.cid)
            )
            is_complete.set()

        except KeyError:
            # If we're cancelled before the task returns then the
            # cancel scope will not have been inserted yet
            if is_rpc:
                log.warning(
                    'RPC task likely errored or cancelled before start?'
                    f'|_{ctx._task}\n'
                    f'  >> {ctx.repr_rpc}\n'
                )
            else:
                log.cancel(
                    'Failed to de-alloc internal runtime cancel task?\n'
                    f'|_{ctx._task}\n'
                    f'  >> {ctx.repr_rpc}\n'
                )

        finally:
            if not actor._rpc_tasks:
                log.runtime('All RPC tasks have completed')
                actor._ongoing_rpc_tasks.set()


async def _invoke(

    actor: Actor,
    cid: str,
    chan: Channel,
    func: Callable,
    kwargs: dict[str, Any],

    is_rpc: bool = True,
    hide_tb: bool = True,
    return_msg: Return|CancelAck = Return,

    task_status: TaskStatus[
        Context | BaseException
    ] = trio.TASK_STATUS_IGNORED,
):
    '''
    Schedule a `trio` task-as-func and deliver result(s) over
    connected IPC channel.

    This is the core "RPC" `trio.Task` scheduling machinery used to start every
    remotely invoked function, normally in `Actor._service_n: Nursery`.

    '''
    __tracebackhide__: bool = hide_tb
    treat_as_gen: bool = False

    if (
        _state.debug_mode()
        and
        _state._runtime_vars['use_greenback']
    ):
        # XXX for .pause_from_sync()` usage we need to make sure
        # `greenback` is boostrapped in the subactor!
        await _debug.maybe_init_greenback()

    # TODO: possibly a specially formatted traceback
    # (not sure what typing is for this..)?
    # tb: TracebackType = None

    cancel_scope = CancelScope()
    cs: CancelScope|None = None  # ref when activated
    ctx = actor.get_context(
        chan=chan,
        cid=cid,
        nsf=NamespacePath.from_ref(func),

        # NOTE: no portal passed bc this is the "child"-side

        # We shouldn't ever need to pass this through right?
        # it's up to the soon-to-be called rpc task to
        # open the stream with this option.
        # allow_overruns=True,
    )
    context: bool = False

    assert not _state._ctxvar_Context.get()

    # TODO: deprecate this style..
    if getattr(func, '_tractor_stream_function', False):
        # handle decorated ``@tractor.stream`` async functions
        sig = inspect.signature(func)
        params = sig.parameters

        # compat with old api
        kwargs['ctx'] = ctx
        treat_as_gen = True

        if 'ctx' in params:
            warnings.warn(
                "`@tractor.stream decorated funcs should now declare "
                "a `stream`  arg, `ctx` is now designated for use with "
                "@tractor.context",
                DeprecationWarning,
                stacklevel=2,
            )

        elif 'stream' in params:
            assert 'stream' in params
            kwargs['stream'] = ctx


    # handle decorated ``@tractor.context`` async function
    elif getattr(func, '_tractor_context_function', False):
        kwargs['ctx'] = ctx
        context = True

    # errors raised inside this block are propgated back to caller
    async with _errors_relayed_via_ipc(
        actor,
        chan,
        ctx,
        is_rpc,
        hide_tb=hide_tb,
        task_status=task_status,
    ):
        if not (
            inspect.isasyncgenfunction(func)
            or
            inspect.iscoroutinefunction(func)
        ):
            raise TypeError(f'{func} must be an async function!')

        # init coroutine with `kwargs` to immediately catch any
        # type-sig errors.
        try:
            coro = func(**kwargs)
        except TypeError:
            raise

        # TODO: impl all these cases in terms of the `Context` one!
        if not context:
            await _invoke_non_context(
                actor,
                cancel_scope,
                ctx,
                cid,
                chan,
                func,
                coro,
                kwargs,
                treat_as_gen,
                is_rpc,
                return_msg,
                task_status,
            )
            # XXX below fallthrough is ONLY for `@context` eps
            return

        # our most general case: a remote SC-transitive,
        # IPC-linked, cross-actor-task "context"
        # ------ - ------
        # TODO: every other "func type" should be implemented from
        # a special case of this impl eventually!
        # -[ ] streaming funcs should instead of being async-for
        #     handled directly here wrapped in
        #     a async-with-open_stream() closure that does the
        #     normal thing you'd expect a far end streaming context
        #     to (if written by the app-dev).
        # -[ ] one off async funcs can literally just be called
        #     here and awaited directly, possibly just with a small
        #     wrapper that calls `Context.started()` and then does
        #     the `await coro()`?

        # ------ - ------
        # a "context" endpoint is the most general and
        # "least sugary" type of RPC with support for
        # bi-dir streaming B)
        #
        # the concurrency relation is simlar to a task nursery
        # wherein a "parent" task (the one that enters
        # `trio.open_nursery()` in some actor "opens" (via
        # `Portal.open_context()`) an IPC ctx to another peer
        # (which is maybe a sub-) actor who then schedules (aka
        # `trio.Nursery.start()`s) a new "child" task to execute
        # the `@context` annotated func; that is this func we're
        # running directly below!
        # ------ - ------
        #
        # StartAck: respond immediately with endpoint info
        await chan.send(
            StartAck(
                cid=cid,
                functype='context',
            )
        )

        # TODO: should we also use an `.open_context()` equiv
        # for this child side by factoring the impl from
        # `Portal.open_context()` into a common helper?
        #
        # NOTE: there are many different ctx state details
        # in a child side instance according to current impl:
        # - `.cancelled_caught` can never be `True`.
        #  -> the below scope is never exposed to the
        #     `@context` marked RPC function.
        # - `._portal` is never set.
        try:
            async with trio.open_nursery() as tn:
                ctx._scope_nursery = tn
                ctx._scope = tn.cancel_scope
                _state._ctxvar_Context.set(ctx)
                task_status.started(ctx)

                # TODO: should would be nice to have our
                # `TaskMngr` nursery here!
                res: Any = await coro
                ctx._result = res

                # deliver final result to caller side.
                await chan.send(
                    return_msg(
                        cid=cid,
                        pld=res,
                    )
                )

            # NOTE: this happens IFF `ctx._scope.cancel()` is
            # called by any of,
            # - *this* child task manually calling `ctx.cancel()`.
            # - the runtime calling `ctx._deliver_msg()` which
            #   itself calls `ctx._maybe_cancel_and_set_remote_error()`
            #   which cancels the scope presuming the input error
            #   is not a `.cancel_acked` pleaser.
            if ctx._scope.cancelled_caught:
                our_uid: tuple = actor.uid

                # first check for and raise any remote error
                # before raising any context cancelled case
                # so that real remote errors don't get masked as
                # ``ContextCancelled``s.
                if re := ctx._remote_error:
                    ctx._maybe_raise_remote_err(re)

                cs: CancelScope = ctx._scope

                if cs.cancel_called:
                    canceller: tuple = ctx.canceller
                    explain: str = f'{ctx.side!r}-side task was cancelled by '

                    # NOTE / TODO: if we end up having
                    # ``Actor._cancel_task()`` call
                    # ``Context.cancel()`` directly, we're going to
                    # need to change this logic branch since it
                    # will always enter..
                    if ctx._cancel_called:
                        # TODO: test for this!!!!!
                        canceller: tuple = our_uid
                        explain += 'itself '

                    # if the channel which spawned the ctx is the
                    # one that cancelled it then we report that, vs.
                    # it being some other random actor that for ex.
                    # some actor who calls `Portal.cancel_actor()`
                    # and by side-effect cancels this ctx.
                    #
                    # TODO: determine if the ctx peer task was the
                    # exact task which cancelled, vs. some other
                    # task in the same actor.
                    elif canceller == ctx.chan.uid:
                        explain += f'its {ctx.peer_side!r}-side peer'

                    else:
                        explain += 'a remote peer'

                    explain += (
                        add_div(message=explain)
                        +
                        f'<= canceller: {canceller}\n'
                        f'=> cancellee: {our_uid}\n'
                        # TODO: better repr for ctx tasks..
                        f'  |_{ctx.side!r} {ctx._task}'

                        # TODO: instead just show the
                        # ctx.__str__() here?
                        # -[ ] textwrap.indent() it correctly!
                        # -[ ] BUT we need to wait until
                        #   the state is filled out before emitting
                        #   this msg right ow its kinda empty? bleh..
                        #
                        # f'  |_{ctx}'
                    )

                    # task-contex was either cancelled by request
                    # using ``Portal.cancel_actor()`` or
                    # ``Context.cancel()`` on the far end, or it
                    # was cancelled by the local child (or callee)
                    # task, so relay this cancel signal to the
                    # other side.
                    ctxc = ContextCancelled(
                        message=explain,
                        boxed_type=trio.Cancelled,
                        canceller=canceller,
                    )
                    # does this matter other then for
                    # consistentcy/testing? |_ no user code should be
                    # in this scope at this point..
                    # ctx._local_error = ctxc
                    raise ctxc

        # XXX: do we ever trigger this block any more?
        except (
            BaseExceptionGroup,
            trio.Cancelled,
            BaseException,

        ) as scope_error:
            if (
                isinstance(scope_error, RuntimeError)
                and scope_error.args
                and 'Cancel scope stack corrupted' in scope_error.args[0]
            ):
                log.exception('Cancel scope stack corrupted!?\n')
                # _debug.mk_pdb().set_trace()

            # always set this (child) side's exception as the
            # local error on the context
            ctx._local_error: BaseException = scope_error

            # if a remote error was set then likely the
            # exception group was raised due to that, so
            # and we instead raise that error immediately!
            ctx.maybe_raise()

            # maybe TODO: pack in come kinda
            # `trio.Cancelled.__traceback__` here so they can be
            # unwrapped and displayed on the caller side? no se..
            raise

        # `@context` entrypoint task bookeeping.
        # i.e. only pop the context tracking if used ;)
        finally:
            assert chan.uid

            # don't pop the local context until we know the
            # associated child isn't in debug any more
            await _debug.maybe_wait_for_debugger()
            ctx: Context = actor._contexts.pop((
                chan.uid,
                cid,
            ))

            merr: Exception|None = ctx.maybe_error
            (
                res_type_str,
                res_str,
            ) = (
                ('error', f'{type(merr)}',) if merr
                else (
                    'result',
                    f'`{repr(ctx.outcome)}`',
                )
            )
            message: str = (
                f'IPC context terminated with a final {res_type_str}\n\n'
                f'{ctx}'
            )
            if merr:
                from tractor import RemoteActorError
                if not isinstance(merr, RemoteActorError):
                    fmt_merr: str = (
                        f'\n{merr!r}\n'
                        # f'{merr.args[0]!r}\n'
                    )
                else:
                    fmt_merr = f'\n{merr!r}'
                log.error(
                    message
                    +
                    fmt_merr
                )
            else:
                log.runtime(message)


async def try_ship_error_to_remote(
    channel: Channel,
    err: Exception|BaseExceptionGroup,

    cid: str|None = None,
    remote_descr: str = 'parent',
    hide_tb: bool = True,

) -> None:
    '''
    Box, pack and encode a local runtime(-internal) exception for
    an IPC channel `.send()` with transport/network failures and
    local cancellation ignored but logged as critical(ly bad).

    '''
    __tracebackhide__: bool = hide_tb
    with CancelScope(shield=True):
        try:
            # NOTE: normally only used for internal runtime errors
            # so ship to peer actor without a cid.
            # msg: dict = pack_error(
            msg: Error = pack_error(
                err,
                cid=cid,

                # TODO: special tb fmting for ctxc cases?
                # tb=tb,
            )
            await channel.send(msg)

        # XXX NOTE XXX in SC terms this is one of the worst things
        # that can happen and provides for a 2-general's dilemma..
        except (
            trio.ClosedResourceError,
            trio.BrokenResourceError,
            BrokenPipeError,
        ):
            log.critical(
                'IPC transport failure -> '
                f'failed to ship error to {remote_descr}!\n\n'
                f'X=> {channel.uid}\n\n'

                # TODO: use `.msg.preetty_struct` for this!
                f'{msg}\n'
            )


async def process_messages(
    actor: Actor,
    chan: Channel,
    shield: bool = False,
    task_status: TaskStatus[CancelScope] = trio.TASK_STATUS_IGNORED,

) -> (
    bool,  # chan diconnected
    MsgType,  # last msg
):
    '''
    This is the low-level, per-IPC-channel, RPC task scheduler loop.

    Receive (multiplexed) per-`Channel` RPC requests as msgs from
    remote processes; schedule target async funcs as local
    `trio.Task`s inside the `Actor._service_n: Nursery`.

    Depending on msg type, non-`cmd` (task spawning/starting)
    request payloads (eg. `started`, `yield`, `return`, `error`)
    are delivered to locally running, linked-via-`Context`, tasks
    with any (boxed) errors and/or final results shipped back to
    the remote side.

    All higher level inter-actor comms ops are delivered in some
    form by the msg processing here, including:

    - lookup and invocation of any (async) funcs-as-tasks requested
      by remote actors presuming the local actor has enabled their
      containing module.

    - IPC-session oriented `Context` and `MsgStream` msg payload
      delivery such as `started`, `yield` and `return` msgs.

    - cancellation handling for both `Context.cancel()` (which
      translate to `Actor._cancel_task()` RPCs server side)
      and `Actor.cancel()` process-wide-runtime-shutdown requests
      (as utilized inside `Portal.cancel_actor()` ).

    '''
    assert actor._service_n  # state sanity

    # TODO: once `trio` get's an "obvious way" for req/resp we
    # should use it?
    # -[ ] existing GH https://github.com/python-trio/trio/issues/467
    # -[ ] for other transports (like QUIC) we can possibly just
    #  entirely avoid the feeder mem-chans since each msg will be
    #  delivered with a ctx-id already?
    #
    #  |_ for ex, from `aioquic` which exposed "stream ids":
    #  - https://github.com/aiortc/aioquic/blob/main/src/aioquic/quic/connection.py#L1175
    #  - https://github.com/aiortc/aioquic/blob/main/src/aioquic/quic/connection.py#L659
    nursery_cancelled_before_task: bool = False
    msg: Msg|None = None
    try:
        # NOTE: this internal scope allows for keeping this
        # message loop running despite the current task having
        # been cancelled (eg. `open_portal()` may call this method
        # from a locally spawned task) and recieve this scope
        # using ``scope = Nursery.start()``
        with CancelScope(shield=shield) as loop_cs:
            task_status.started(loop_cs)

            async for msg in chan:
                log.transport(   # type: ignore
                    f'IPC msg from peer\n'
                    f'<= {chan.uid}\n\n'

                    # TODO: avoid fmting depending on loglevel for perf?
                    # -[ ] specifically `pretty_struct.pformat()` sub-call..?
                    #   - how to only log-level-aware actually call this?
                    # -[ ] use `.msg.pretty_struct` here now instead!
                    # f'{pretty_struct.pformat(msg)}\n'
                    f'{msg}\n'
                )

                match msg:
                    # msg for an ongoing IPC ctx session, deliver msg to
                    # local task.
                    case (
                        StartAck(cid=cid)
                        | Started(cid=cid)
                        | Yield(cid=cid)
                        | Stop(cid=cid)
                        | Return(cid=cid)
                        | CancelAck(cid=cid)

                        # `.cid` indicates RPC-ctx-task scoped
                        | Error(cid=cid)

                        # recv-side `MsgType` decode violation
                        | MsgTypeError(cid=cid)
                    ):
                        # deliver response to local caller/waiter
                        # via its per-remote-context memory channel.
                        await actor._deliver_ctx_payload(
                            chan,
                            cid,
                            msg,
                        )

                    # `Actor`(-internal) runtime cancel requests
                    case Start(
                        ns='self',
                        func='cancel',
                        cid=cid,
                        kwargs=kwargs,
                    ):
                        kwargs |= {'req_chan': chan}

                        # XXX NOTE XXX don't start entire actor
                        # runtime cancellation if this actor is
                        # currently in debug mode!
                        pdb_complete: trio.Event|None = _debug.DebugStatus.repl_release
                        if pdb_complete:
                            await pdb_complete.wait()

                        # Either of  `Actor.cancel()`/`.cancel_soon()`
                        # was called, so terminate this IPC msg
                        # loop, exit back out into `async_main()`,
                        # and immediately start the core runtime
                        # machinery shutdown!
                        with CancelScope(shield=True):
                            await _invoke(
                                actor,
                                cid,
                                chan,
                                actor.cancel,
                                kwargs,
                                is_rpc=False,
                                return_msg=CancelAck,
                            )

                        log.runtime(
                            'Cancelling IPC transport msg-loop with peer:\n'
                            f'|_{chan}\n'
                        )
                        loop_cs.cancel()
                        break

                    case Start(
                        ns='self',
                        func='_cancel_task',
                        cid=cid,
                        kwargs=kwargs,
                    ):
                        target_cid: str = kwargs['cid']
                        kwargs |= {
                            'requesting_uid': chan.uid,
                            'ipc_msg': msg,

                            # XXX NOTE! ONLY the rpc-task-owning
                            # parent IPC channel should be able to
                            # cancel it!
                            'parent_chan': chan,
                        }
                        try:
                            await _invoke(
                                actor,
                                cid,
                                chan,
                                actor._cancel_task,
                                kwargs,
                                is_rpc=False,
                                return_msg=CancelAck,
                            )
                        except BaseException:
                            log.exception(
                                'Failed to cancel task?\n'
                                f'<= canceller: {chan.uid}\n'
                                f'  |_{chan}\n\n'
                                f'=> {actor}\n'
                                f'  |_cid: {target_cid}\n'
                            )

                    # the "MAIN" RPC endpoint to schedule-a-`trio.Task`
                    #                ------ - ------
                    # -[x] discard un-authed msgs as per,
                    #    <TODO put issue for typed msging structs>
                    case Start(
                        cid=cid,
                        ns=ns,
                        func=funcname,
                        kwargs=kwargs,  # type-spec this? see `msg.types`
                        uid=actorid,
                    ):
                        start_status: str = (
                            'Handling RPC `Start` request\n'
                            f'<= peer: {actorid}\n\n'
                            f'  |_{chan}\n'
                            f'  |_cid: {cid}\n\n'
                            # f'  |_{ns}.{funcname}({kwargs})\n'
                            f'>> {actor.uid}\n'
                            f'  |_{actor}\n'
                            f'   -> nsp: `{ns}.{funcname}({kwargs})`\n'

                            # f'  |_{ns}.{funcname}({kwargs})\n\n'

                            # f'{pretty_struct.pformat(msg)}\n'
                        )

                        # runtime-internal endpoint: `Actor.<funcname>`
                        # only registry methods exist now yah,
                        # like ``.register_actor()`` etc. ?
                        if ns == 'self':
                            func: Callable = getattr(actor, funcname)

                        # application RPC endpoint
                        else:
                            try:
                                func: Callable = actor._get_rpc_func(
                                    ns,
                                    funcname,
                                )
                            except (
                                ModuleNotExposed,
                                AttributeError,
                            ) as err:
                                # always complain to requester
                                # client about un-enabled modules
                                err_msg: dict[str, dict] = pack_error(
                                    err,
                                    cid=cid,
                                )
                                await chan.send(err_msg)
                                continue

                        start_status += (
                            f'   -> func: {func}\n'
                        )

                        # schedule a task for the requested RPC function
                        # in the actor's main "service nursery".
                        #
                        # TODO: possibly a service-tn per IPC channel for
                        # supervision isolation? would avoid having to
                        # manage RPC tasks individually in `._rpc_tasks`
                        # table?
                        start_status += '   -> scheduling new task..\n'
                        log.runtime(start_status)
                        try:
                            ctx: Context = await actor._service_n.start(
                                partial(
                                    _invoke,
                                    actor,
                                    cid,
                                    chan,
                                    func,
                                    kwargs,
                                ),
                                name=funcname,
                            )

                        except (
                            RuntimeError,
                            BaseExceptionGroup,
                        ):
                            # avoid reporting a benign race condition
                            # during actor runtime teardown.
                            nursery_cancelled_before_task: bool = True
                            break

                        # in the lone case where a ``Context`` is not
                        # delivered, it's likely going to be a locally
                        # scoped exception from ``_invoke()`` itself.
                        if isinstance(err := ctx, Exception):
                            log.warning(
                                start_status
                                +
                                '   -> task for RPC failed?\n\n'
                                f'{err}'
                            )
                            continue

                        else:
                            # mark our global state with ongoing rpc tasks
                            actor._ongoing_rpc_tasks = trio.Event()

                            # store cancel scope such that the rpc task can be
                            # cancelled gracefully if requested
                            actor._rpc_tasks[(chan, cid)] = (
                                ctx,
                                func,
                                trio.Event(),
                            )

                    # runtime-scoped remote (internal) error
                    # (^- bc no `Error.cid` -^)
                    #
                    # NOTE: this is the non-rpc error case, that
                    # is, an error NOT raised inside a call to
                    # `_invoke()` (i.e. no cid was provided in the
                    # msg - see above). Raise error inline and
                    # mark the channel as "globally errored" for
                    # all downstream consuming primitives.
                    case Error():
                        chan._exc: Exception = unpack_error(
                            msg,
                            chan=chan,
                        )
                        raise chan._exc

                    # unknown/invalid msg type?
                    case _:
                        codec: MsgCodec = current_codec()
                        message: str = (
                            f'Unhandled IPC msg for codec?\n\n'
                            f'|_{codec}\n\n'
                            f'{msg}\n'
                        )
                        log.exception(message)
                        raise RuntimeError(message)

                log.transport(
                    'Waiting on next IPC msg from\n'
                    f'peer: {chan.uid}\n'
                    f'|_{chan}\n'
                )

            # END-OF `async for`:
            # IPC disconnected via `trio.EndOfChannel`, likely
            # due to a (graceful) `Channel.aclose()`.
            log.runtime(
                f'channel for {chan.uid} disconnected, cancelling RPC tasks\n'
                f'|_{chan}\n'
            )
            await actor.cancel_rpc_tasks(
                req_uid=actor.uid,
                # a "self cancel" in terms of the lifetime of the
                # IPC connection which is presumed to be the
                # source of any requests for spawned tasks.
                parent_chan=chan,
            )

    except TransportClosed:
        # channels "breaking" (for TCP streams by EOF or 104
        # connection-reset) is ok since we don't have a teardown
        # handshake for them (yet) and instead we simply bail out of
        # the message loop and expect the teardown sequence to clean
        # up..
        #
        # TODO: maybe add a teardown handshake? and,
        # -[ ] don't show this msg if it's an ephemeral discovery ep call?
        # -[ ] figure out how this will break with other transports?
        log.runtime(
            f'IPC channel closed abruptly\n'
            f'<=x peer: {chan.uid}\n'
            f'   |_{chan.raddr}\n'
        )

        # transport **WAS** disconnected
        return (True, msg)

    except (
        Exception,
        BaseExceptionGroup,
    ) as err:

        if nursery_cancelled_before_task:
            sn: Nursery = actor._service_n
            assert sn and sn.cancel_scope.cancel_called  # sanity
            log.cancel(
                f'Service nursery cancelled before it handled {funcname}'
            )
        else:
            # ship any "internal" exception (i.e. one from internal
            # machinery not from an rpc task) to parent
            match err:
                case ContextCancelled():
                    log.cancel(
                        f'Actor: {actor.uid} was context-cancelled with,\n'
                        f'str(err)'
                    )
                case _:
                    log.exception("Actor errored:")

            if actor._parent_chan:
                await try_ship_error_to_remote(
                    actor._parent_chan,
                    err,
                )

        # if this is the `MainProcess` we expect the error broadcasting
        # above to trigger an error at consuming portal "checkpoints"
        raise

    finally:
        # msg debugging for when he machinery is brokey
        if msg is None:
            message: str = 'Exiting IPC msg loop without receiving a msg?'
        else:
            message: str = (
                'Exiting IPC msg loop with final msg\n\n'
                f'<= peer: {chan.uid}\n'
                f'  |_{chan}\n\n'
                f'{pretty_struct.pformat(msg)}'
            )

        log.runtime(message)

    # transport **WAS NOT** disconnected
    return (False, msg)
