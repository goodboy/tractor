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
The fundamental cross process SC abstraction: an inter-actor,
cancel-scope linked task "context".

A ``Context`` is very similar to the ``trio.Nursery.cancel_scope`` built
into each ``trio.Nursery`` except it links the lifetimes of memory space
disjoint, parallel executing tasks in separate actors.

'''
from __future__ import annotations
from collections import deque
from contextlib import asynccontextmanager as acm
from dataclasses import (
    dataclass,
    field,
)
from functools import partial
import inspect
from pprint import pformat
from typing import (
    Any,
    Callable,
    AsyncGenerator,
    TYPE_CHECKING,
)
import warnings

import trio

# from .devx import (
#     maybe_wait_for_debugger,
#     pause,
# )
from .msg import NamespacePath
from ._exceptions import (
    # _raise_from_no_key_in_msg,
    unpack_error,
    pack_error,
    ContextCancelled,
    # MessagingError,
    RemoteActorError,
    StreamOverrun,
)
from .log import get_logger
from ._ipc import Channel
from ._streaming import MsgStream
from ._state import current_actor

if TYPE_CHECKING:
    from ._portal import Portal
    from ._runtime import Actor


log = get_logger(__name__)


async def _drain_to_final_msg(
    ctx: Context,

    msg_limit: int = 6,

) -> list[dict]:
    '''
    Drain IPC msgs delivered to the underlying rx-mem-chan
    `Context._recv_chan` from the runtime in search for a final
    result or error msg.

    The motivation here is to ideally capture errors during ctxc
    conditions where a canc-request/or local error is sent but the
    local task also excepts and enters the
    `Portal.open_context().__aexit__()` block wherein we prefer to
    capture and raise any remote error or ctxc-ack as part of the
    `ctx.result()` cleanup and teardown sequence.

    '''
    raise_overrun: bool = not ctx._allow_overruns

    # wait for a final context result by collecting (but
    # basically ignoring) any bi-dir-stream msgs still in transit
    # from the far end.
    pre_result_drained: list[dict] = []
    while not ctx._remote_error:
        try:
            # NOTE: this REPL usage actually works here dawg! Bo
            # from .devx._debug import pause
            # await pause()
            if re := ctx._remote_error:
                ctx._maybe_raise_remote_err(
                    re,
                    # NOTE: obvi we don't care if we
                    # overran the far end if we're already
                    # waiting on a final result (msg).
                    raise_overrun_from_self=raise_overrun,
                )

            # TODO: bad idea?
            # with trio.CancelScope() as res_cs:
            #     ctx._res_scope = res_cs
            #     msg: dict = await ctx._recv_chan.receive()
            # if res_cs.cancelled_caught:

            # from .devx._debug import pause
            # await pause()
            msg: dict = await ctx._recv_chan.receive()
            ctx._result: Any = msg['return']
            log.runtime(
                'Context delivered final draining msg:\n'
                f'{pformat(msg)}'
            )
            pre_result_drained.append(msg)
            # NOTE: we don't need to do this right?
            # XXX: only close the rx mem chan AFTER
            # a final result is retreived.
            # if ctx._recv_chan:
            #     await ctx._recv_chan.aclose()
            break

        # NOTE: we get here if the far end was
        # `ContextCancelled` in 2 cases:
        # 1. we requested the cancellation and thus
        #    SHOULD NOT raise that far end error,
        # 2. WE DID NOT REQUEST that cancel and thus
        #    SHOULD RAISE HERE!
        except trio.Cancelled:

            # CASE 2: mask the local cancelled-error(s)
            # only when we are sure the remote error is
            # the source cause of this local task's
            # cancellation.
            if re := ctx._remote_error:
                ctx._maybe_raise_remote_err(re)

            # CASE 1: we DID request the cancel we simply
            # continue to bubble up as normal.
            raise

        except KeyError:

            if 'yield' in msg:
                # far end task is still streaming to us so discard
                # and report per local context state.
                if (
                    (ctx._stream.closed
                     and (reason := 'stream was already closed')
                    )
                    or (ctx._cancel_called
                        and (reason := 'ctx called `.cancel()`')
                    )
                    or (ctx._cancelled_caught
                        and (reason := 'ctx caught a cancel')
                    )
                    or (len(pre_result_drained) > msg_limit
                        and (reason := f'"yield" limit={msg_limit}')
                    )
                ):
                    log.cancel(
                        'Cancelling `MsgStream` drain since '
                        f'{reason}\n\n'
                        f'<= {ctx.chan.uid}\n'
                        f'  |_{ctx._nsf}()\n\n'
                        f'=> {ctx._task}\n'
                        f'  |_{ctx._stream}\n\n'

                        f'{pformat(msg)}\n'
                    )
                    return pre_result_drained

                # drain up to the `msg_limit` hoping to get
                # a final result or error/ctxc.
                else:
                    log.warning(
                        'Ignoring "yield" msg during `ctx.result()` drain..\n'
                        f'<= {ctx.chan.uid}\n'
                        f'  |_{ctx._nsf}()\n\n'
                        f'=> {ctx._task}\n'
                        f'  |_{ctx._stream}\n\n'

                        f'{pformat(msg)}\n'
                    )
                    pre_result_drained.append(msg)
                    continue

            # TODO: work out edge cases here where
            # a stream is open but the task also calls
            # this?
            # -[ ] should be a runtime error if a stream is open
            #   right?
            elif 'stop' in msg:
                log.cancel(
                    'Remote stream terminated due to "stop" msg:\n\n'
                    f'{pformat(msg)}\n'
                )
                pre_result_drained.append(msg)
                continue

            # internal error should never get here
            assert msg.get('cid'), (
                "Received internal error at portal?"
            )

            # XXX fallthrough to handle expected error XXX
            re: Exception|None = ctx._remote_error
            if re:
                log.critical(
                    'Remote ctx terminated due to "error" msg:\n'
                    f'{re}'
                )
                assert msg is ctx._cancel_msg
                # NOTE: this solved a super dupe edge case XD
                # this was THE super duper edge case of:
                # - local task opens a remote task,
                # - requests remote cancellation of far end
                #   ctx/tasks,
                # - needs to wait for the cancel ack msg
                #   (ctxc) or some result in the race case
                #   where the other side's task returns
                #   before the cancel request msg is ever
                #   rxed and processed,
                # - here this surrounding drain loop (which
                #   iterates all ipc msgs until the ack or
                #   an early result arrives) was NOT exiting
                #   since we are the edge case: local task
                #   does not re-raise any ctxc it receives
                #   IFF **it** was the cancellation
                #   requester..
                # will raise if necessary, ow break from
                # loop presuming any error terminates the
                # context!
                ctx._maybe_raise_remote_err(
                    re,
                    # NOTE: obvi we don't care if we
                    # overran the far end if we're already
                    # waiting on a final result (msg).
                    # raise_overrun_from_self=False,
                    raise_overrun_from_self=raise_overrun,
                )

                break  # OOOOOF, yeah obvi we need this..

            # XXX we should never really get here
            # right! since `._deliver_msg()` should
            # always have detected an {'error': ..}
            # msg and already called this right!?!
            elif error := unpack_error(
                msg=msg,
                chan=ctx._portal.channel,
                hide_tb=False,
            ):
                log.critical('SHOULD NEVER GET HERE!?')
                assert msg is ctx._cancel_msg
                assert error.msgdata == ctx._remote_error.msgdata
                from .devx._debug import pause
                await pause()
                ctx._maybe_cancel_and_set_remote_error(error)
                ctx._maybe_raise_remote_err(error)

            else:
                # bubble the original src key error
                raise

    return pre_result_drained


# TODO: make this a msgspec.Struct!
@dataclass
class Context:
    '''
    An inter-actor, SC transitive, `trio.Task` communication context.

    NB: This class should **never be instatiated directly**, it is allocated
    by the runtime in 2 ways:
     - by entering ``Portal.open_context()`` which is the primary
       public API for any "caller" task or,
     - by the RPC machinery's `._runtime._invoke()` as a `ctx` arg
       to a remotely scheduled "callee" function.

    AND is always constructed using the below ``mk_context()``.

    Allows maintaining task or protocol specific state between
    2 cancel-scope-linked, communicating and parallel executing
    `trio.Task`s. Contexts are allocated on each side of any task
    RPC-linked msg dialog, i.e. for every request to a remote
    actor from a `Portal`. On the "callee" side a context is
    always allocated inside ``._runtime._invoke()``.

    # TODO: more detailed writeup on cancellation, error and
    # streaming semantics..

    A context can be cancelled and (possibly eventually restarted) from
    either side of the underlying IPC channel, it can also open task
    oriented message streams,  and acts more or less as an IPC aware
    inter-actor-task ``trio.CancelScope``.

    '''
    chan: Channel
    cid: str  # "context id", more or less a unique linked-task-pair id
    # the "feeder" channels for delivering message values to the
    # local task from the runtime's msg processing loop.
    _recv_chan: trio.MemoryReceiveChannel
    _send_chan: trio.MemorySendChannel

    # full "namespace-path" to target RPC function
    _nsf: NamespacePath

    # the "invocation type" of the far end task-entry-point
    # function, normally matching a logic block inside
    # `._runtime.invoke()`.
    _remote_func_type: str | None = None

    # NOTE: (for now) only set (a portal) on the caller side since
    # the callee doesn't generally need a ref to one and should
    # normally need to explicitly ask for handle to its peer if
    # more the the `Context` is needed?
    _portal: Portal | None = None

    # NOTE: each side of the context has its own cancel scope
    # which is exactly the primitive that allows for
    # cross-actor-task-supervision and thus SC.
    _scope: trio.CancelScope | None = None
    _task: trio.lowlevel.Task|None = None
    # _res_scope: trio.CancelScope|None = None

    # on a clean exit there should be a final value
    # delivered from the far end "callee" task, so
    # this value is only set on one side.
    _result: Any | int = None

    # if the local "caller"  task errors this
    # value is always set to the error that was
    # captured in the `Portal.open_context().__aexit__()`
    # teardown.
    _local_error: BaseException | None = None

    # if the either side gets an error from the other
    # this value is set to that error unpacked from an
    # IPC msg.
    _remote_error: BaseException | None = None

    # only set if the local task called `.cancel()`
    _cancel_called: bool = False  # did WE cancel the far end?

    # TODO: do we even need this? we can assume that if we're
    # cancelled that the other side is as well, so maybe we should
    # instead just have a `.canceller` pulled from the
    # `ContextCancelled`?
    _canceller: tuple[str, str] | None = None

    # NOTE: we try to ensure assignment of a "cancel msg" since
    # there's always going to be an "underlying reason" that any
    # context was closed due to either a remote side error or
    # a call to `.cancel()` which triggers `ContextCancelled`.
    _cancel_msg: str | dict | None = None

    # NOTE: this state var used by the runtime to determine if the
    # `pdbp` REPL is allowed to engage on contexts terminated via
    # a `ContextCancelled` due to a call to `.cancel()` triggering
    # "graceful closure" on either side:
    # - `._runtime._invoke()` will check this flag before engaging
    #   the crash handler REPL in such cases where the "callee"
    #   raises the cancellation,
    # - `.devx._debug.lock_tty_for_child()` will set it to `False` if
    #   the global tty-lock has been configured to filter out some
    #   actors from being able to acquire the debugger lock.
    _enter_debugger_on_cancel: bool = True

    @property
    def cancel_called(self) -> bool:
        '''
        Records whether cancellation has been requested for this context
        by either an explicit call to  ``.cancel()`` or an implicit call
        due to an error caught inside the ``Portal.open_context()``
        block.

        '''
        return self._cancel_called

    @property
    def canceller(self) -> tuple[str, str] | None:
        '''
        ``Actor.uid: tuple[str, str]`` of the (remote)
        actor-process who's task was cancelled thus causing this
        (side of the) context to also be cancelled.

        '''
        return self._canceller

    @property
    def cancelled_caught(self) -> bool:
        return (
            # the local scope was cancelled either by
            # remote error or self-request
            self._scope.cancelled_caught

            # the local scope was never cancelled
            # and instead likely we received a remote side
            # cancellation that was raised inside `.result()`
            or (
                (se := self._local_error)
                and
                isinstance(se, ContextCancelled)
                and (
                    se.canceller == self.canceller
                    or
                    se is self._remote_error
                )
            )
        )

    # @property
    # def is_waiting_result(self) -> bool:
    #     return bool(self._res_scope)

    @property
    def side(self) -> str:
        '''
        Return string indicating which task this instance is wrapping.

        '''
        return 'caller' if self._portal else 'callee'

    # init and streaming state
    _started_called: bool = False
    _stream_opened: bool = False
    _stream: MsgStream|None = None

    # overrun handling machinery
    # NOTE: none of this provides "backpressure" to the remote
    # task, only an ability to not lose messages when the local
    # task is configured to NOT transmit ``StreamOverrun``s back
    # to the other side.
    _overflow_q: deque[dict] = field(
        default_factory=partial(
            deque,
            maxlen=616,
        )
    )
    _scope_nursery: trio.Nursery | None = None
    _in_overrun: bool = False
    _allow_overruns: bool = False

    async def send_yield(
        self,
        data: Any,

    ) -> None:

        warnings.warn(
            "`Context.send_yield()` is now deprecated. "
            "Use ``MessageStream.send()``. ",
            DeprecationWarning,
            stacklevel=2,
        )
        await self.chan.send({'yield': data, 'cid': self.cid})

    async def send_stop(self) -> None:
        # await pause()
        await self.chan.send({
            'stop': True,
            'cid': self.cid
        })

    def _maybe_cancel_and_set_remote_error(
        self,
        error: BaseException,

    ) -> None:
        '''
        (Maybe) cancel this local scope due to a received remote
        error (normally via an IPC msg) which the actor runtime
        routes to this context.

        Acts as a form of "relay" for a remote error raised in the
        corresponding remote task's `Context` wherein the next time
        the local task exectutes a checkpoint, a `trio.Cancelled`
        will be raised and depending on the type and source of the
        original remote error, and whether or not the local task
        called `.cancel()` itself prior, an equivalent
        `ContextCancelled` or `RemoteActorError` wrapping the
        remote error may be raised here by any of,

        - `Portal.open_context()`
        - `Portal.result()`
        - `Context.open_stream()`
        - `Context.result()`

        when called/closed by actor local task(s).

        NOTEs & TODOs: 
          - It is expected that the caller has previously unwrapped
            the remote error using a call to `unpack_error()` and
            provides that output exception value as the input
            `error` argument here.
          - If this is an error message from a context opened by
            `Portal.open_context()` we want to interrupt any
            ongoing local tasks operating within that `Context`'s
            cancel-scope so as to be notified ASAP of the remote
            error and engage any caller handling (eg. for
            cross-process task supervision).
          - In some cases we may want to raise the remote error
            immediately since there is no guarantee the locally
            operating task(s) will attempt to execute a checkpoint
            any time soon; in such cases there are 2 possible
            approaches depending on the current task's work and
            wrapping "thread" type:

            - `trio`-native-and-graceful: only ever wait for tasks
              to exec a next `trio.lowlevel.checkpoint()` assuming
              that any such task must do so to interact with the
              actor runtime and IPC interfaces.

            - (NOT IMPLEMENTED) system-level-aggressive: maybe we
              could eventually interrupt sync code (invoked using
              `trio.to_thread` or some other adapter layer) with
              a signal (a custom unix one for example?
              https://stackoverflow.com/a/5744185) depending on the
              task's wrapping thread-type such that long running
              sync code should never cause the delay of actor
              supervision tasks such as cancellation and respawn
              logic.

        '''
        # XXX: currently this should only be used when
        # `Portal.open_context()` has been opened since it's
        # assumed that other portal APIs like,
        #  - `Portal.run()`,
        #  - `ActorNursery.run_in_actor()`
        # do their own error checking at their own call points and
        # result processing.

        # XXX: set the remote side's error so that after we cancel
        # whatever task is the opener of this context it can raise
        # that error as the reason.
        # if self._remote_error:
        #     return

        # breakpoint()
        log.cancel(
            'Setting remote error for ctx \n'
            f'<= remote ctx uid: {self.chan.uid}\n'
            f'=>\n{error}'
        )
        self._remote_error: BaseException = error

        if (
            isinstance(error, ContextCancelled)
        ):
            log.cancel(
                'Remote task-context was cancelled for '
                f'actor: {self.chan.uid}\n'
                f'task: {self.cid}\n'
                f'canceller: {error.canceller}\n'
            )
            # always record the cancelling actor's uid since its cancellation
            # state is linked and we want to know which process was
            # the cause / requester of the cancellation.
            # if error.canceller is None:
            #     import pdbp; pdbp.set_trace()

                # breakpoint()
            self._canceller = error.canceller


            if self._cancel_called:
                # this is an expected cancel request response message
                # and we **don't need to raise it** in local cancel
                # scope since it will potentially override a real error.
                return

        else:
            log.error(
                f'Remote context error:\n'
                f'{error}\n'
                f'{pformat(self)}\n'
                # f'remote actor: {self.chan.uid}\n'
                # f'cid: {self.cid}\n'
            )
            self._canceller = self.chan.uid

        # TODO: tempted to **not** do this by-reraising in a
        # nursery and instead cancel a surrounding scope, detect
        # the cancellation, then lookup the error that was set?
        # YES! this is way better and simpler!
        cs: trio.CancelScope = self._scope
        if (
            cs
            and not cs.cancel_called
            and not cs.cancelled_caught
        ):

            # TODO: we can for sure drop this right?
            # from trio.testing import wait_all_tasks_blocked
            # await wait_all_tasks_blocked()

            # TODO: it'd sure be handy to inject our own
            # `trio.Cancelled` subtype here ;)
            # https://github.com/goodboy/tractor/issues/368
            self._scope.cancel()

            # NOTE: this REPL usage actually works here dawg! Bo
            # await pause()

        # TODO: maybe we have to use `._res_scope.cancel()` if it
        # exists?

    async def cancel(
        self,
        timeout: float = 0.616,

    ) -> None:
        '''
        Cancel this inter-actor-task context.

        Request that the far side cancel it's current linked context,
        Timeout quickly in an attempt to sidestep 2-generals...

        '''
        side: str = self.side
        self._cancel_called: bool = True

        header: str = f'Cancelling "{side.upper()}"-side of ctx with peer\n'
        reminfo: str = (
            f'uid: {self.chan.uid}\n'
            f'    |_ {self._nsf}()\n'
        )

        # caller side who entered `Portal.open_context()`
        # NOTE: on the call side we never manually call
        # `._scope.cancel()` since we expect the eventual
        # `ContextCancelled` from the other side to trigger this
        # when the runtime finally receives it during teardown
        # (normally in `.result()` called from
        # `Portal.open_context().__aexit__()`)
        if side == 'caller':
            if not self._portal:
                raise RuntimeError(
                    "No portal found, this is likely a callee side context"
                )

            cid: str = self.cid
            with trio.move_on_after(timeout) as cs:
                cs.shield = True
                log.cancel(
                    header
                    +
                    reminfo
                )

                # NOTE: we're telling the far end actor to cancel a task
                # corresponding to *this actor*. The far end local channel
                # instance is passed to `Actor._cancel_task()` implicitly.
                await self._portal.run_from_ns(
                    'self',
                    '_cancel_task',
                    cid=cid,
                )

            if cs.cancelled_caught:
                # XXX: there's no way to know if the remote task was indeed
                # cancelled in the case where the connection is broken or
                # some other network error occurred.
                # if not self._portal.channel.connected():
                if not self.chan.connected():
                    log.cancel(
                        'May have failed to cancel remote task?\n'
                        f'{reminfo}'
                    )
                else:
                    log.cancel(
                        'Timed out on cancel request of remote task?\n'
                        f'{reminfo}'
                    )

        # callee side remote task
        # NOTE: on this side we ALWAYS cancel the local scope since
        # the caller expects a `ContextCancelled` to be sent from
        # `._runtime._invoke()` back to the other side.
        else:
            log.cancel(
                header
                +
                reminfo
            )
            # TODO: should we have an explicit cancel message
            # or is relaying the local `trio.Cancelled` as an
            # {'error': trio.Cancelled, cid: "blah"} enough?
            # This probably gets into the discussion in
            # https://github.com/goodboy/tractor/issues/36
            assert self._scope
            self._scope.cancel()

    @acm
    async def open_stream(
        self,
        allow_overruns: bool | None = False,
        msg_buffer_size: int | None = None,

    ) -> AsyncGenerator[MsgStream, None]:
        '''
        Open a ``MsgStream``, a bi-directional stream connected to the
        cross-actor (far end) task for this ``Context``.

        This context manager must be entered on both the caller and
        callee for the stream to logically be considered "connected".

        A ``MsgStream`` is currently "one-shot" use, meaning if you
        close it you can not "re-open" it for streaming and instead you
        must re-establish a new surrounding ``Context`` using
        ``Portal.open_context()``.  In the future this may change but
        currently there seems to be no obvious reason to support
        "re-opening":
          - pausing a stream can be done with a message.
          - task errors will normally require a restart of the entire
            scope of the inter-actor task context due to the nature of
            ``trio``'s cancellation system.

        '''
        actor: Actor = current_actor()

        # If the surrounding context has been cancelled by some
        # task with a handle to THIS, we error here immediately
        # since it likely means the surrounding lexical-scope has
        # errored, been `trio.Cancelled` or at the least
        # `Context.cancel()` was called by some task.
        if self._cancel_called:

            # XXX NOTE: ALWAYS RAISE any remote error here even if
            # it's an expected `ContextCancelled` due to a local
            # task having called `.cancel()`!
            #
            # WHY: we expect the error to always bubble up to the
            # surrounding `Portal.open_context()` call and be
            # absorbed there (silently) and we DO NOT want to
            # actually try to stream - a cancel msg was already
            # sent to the other side!
            if self._remote_error:
                # NOTE: this is diff then calling
                # `._maybe_raise_from_remote_msg()` specifically
                # because any task entering this `.open_stream()`
                # AFTER cancellation has already been requested,
                # we DO NOT want to absorb any ctxc ACK silently!
                raise self._remote_error

            # XXX NOTE: if no `ContextCancelled` has been responded
            # back from the other side (yet), we raise a different
            # runtime error indicating that this task's usage of
            # `Context.cancel()` and then `.open_stream()` is WRONG!
            task: str = trio.lowlevel.current_task().name
            raise RuntimeError(
                'Stream opened after `Context.cancel()` called..?\n'
                f'task: {actor.uid[0]}:{task}\n'
                f'{self}'
            )

        if (
            not self._portal
            and not self._started_called
        ):
            raise RuntimeError(
                'Context.started()` must be called before opening a stream'
            )

        # NOTE: in one way streaming this only happens on the
        # caller side inside `Actor.start_remote_task()` so if you try
        # to send a stop from the caller to the callee in the
        # single-direction-stream case you'll get a lookup error
        # currently.
        ctx: Context = actor.get_context(
            chan=self.chan,
            cid=self.cid,
            nsf=self._nsf,
            msg_buffer_size=msg_buffer_size,
            allow_overruns=allow_overruns,
        )
        ctx._allow_overruns: bool = allow_overruns
        assert ctx is self

        # XXX: If the underlying channel feeder receive mem chan has
        # been closed then likely client code has already exited
        # a ``.open_stream()`` block prior or there was some other
        # unanticipated error or cancellation from ``trio``.

        if ctx._recv_chan._closed:
            raise trio.ClosedResourceError(
                'The underlying channel for this stream was already closed!\n'
            )

        # NOTE: implicitly this will call `MsgStream.aclose()` on
        # `.__aexit__()` due to stream's parent `Channel` type!
        #
        # XXX NOTE XXX: ensures the stream is "one-shot use",
        # which specifically means that on exit,
        # - signal ``trio.EndOfChannel``/``StopAsyncIteration`` to
        #   the far end indicating that the caller exited
        #   the streaming context purposefully by letting
        #   the exit block exec.
        # - this is diff from the cancel/error case where
        #   a cancel request from this side or an error
        #   should be sent to the far end indicating the
        #   stream WAS NOT just closed normally/gracefully.
        async with MsgStream(
            ctx=self,
            rx_chan=ctx._recv_chan,
        ) as stream:

            # NOTE: we track all existing streams per portal for
            # the purposes of attempting graceful closes on runtime
            # cancel requests.
            if self._portal:
                self._portal._streams.add(stream)

            try:
                self._stream_opened: bool = True
                self._stream = stream

                # XXX: do we need this?
                # ensure we aren't cancelled before yielding the stream
                # await trio.lowlevel.checkpoint()
                yield stream


                # XXX: (MEGA IMPORTANT) if this is a root opened process we
                # wait for any immediate child in debug before popping the
                # context from the runtime msg loop otherwise inside
                # ``Actor._push_result()`` the msg will be discarded and in
                # the case where that msg is global debugger unlock (via
                # a "stop" msg for a stream), this can result in a deadlock
                # where the root is waiting on the lock to clear but the
                # child has already cleared it and clobbered IPC.
                #
                # await maybe_wait_for_debugger()

                # XXX TODO: pretty sure this isn't needed (see
                # note above this block) AND will result in
                # a double `.send_stop()` call. The only reason to
                # put it here would be to due with "order" in
                # terms of raising any remote error (as per
                # directly below) or bc the stream's
                # `.__aexit__()` block might not get run
                # (doubtful)? Either way if we did put this back
                # in we also need a state var to avoid the double
                # stop-msg send..
                #
                # await stream.aclose()

                # if re := ctx._remote_error:
                #     ctx._maybe_raise_remote_err(
                #         re,
                #         raise_ctxc_from_self_call=True,
                #     )
                # await trio.lowlevel.checkpoint()

            finally:
                if self._portal:
                    try:
                        self._portal._streams.remove(stream)
                    except KeyError:
                        log.warning(
                            f'Stream was already destroyed?\n'
                            f'actor: {self.chan.uid}\n'
                            f'ctx id: {self.cid}'
                        )

    def _maybe_raise_remote_err(
        self,
        err: Exception,
        raise_ctxc_from_self_call: bool = False,
        raise_overrun_from_self: bool = True,

    ) -> ContextCancelled|None:
        '''
        Maybe raise a remote error depending on who (which task from
        which actor) requested a cancellation (if any).

        '''
        # NOTE: whenever the context's "opener" side (task) **is**
        # the side which requested the cancellation (likekly via
        # ``Context.cancel()``), we don't want to re-raise that
        # cancellation signal locally (would be akin to
        # a ``trio.Nursery`` nursery raising ``trio.Cancelled``
        # whenever  ``CancelScope.cancel()`` was called) and
        # instead silently reap the expected cancellation
        # "error"-msg.
        our_uid: tuple[str, str] = current_actor().uid
        if (
            (not raise_ctxc_from_self_call
             and isinstance(err, ContextCancelled)
             and (
                self._cancel_called
                or self.chan._cancel_called
                or self.canceller == our_uid
                or tuple(err.canceller) == our_uid)
            )
            or
            (not raise_overrun_from_self
             and isinstance(err, RemoteActorError)
             and err.msgdata['type_str'] == 'StreamOverrun'
             and tuple(err.msgdata['sender']) == our_uid
            )

        ):
            # NOTE: we set the local scope error to any "self
            # cancellation" error-response thus "absorbing"
            # the error silently B)
            if self._local_error is None:
                self._local_error = err

            return err

        # NOTE: currently we are masking underlying runtime errors
        # which are often superfluous to user handler code. not
        # sure if this is still needed / desired for all operation?
        # TODO: maybe we can only NOT mask if:
        # - [ ] debug mode is enabled or,
        # - [ ] a certain log level is set?
        # - [ ] consider using `.with_traceback()` to filter out
        #       runtime frames from the tb explicitly?
        # https://docs.python.org/3/reference/simple_stmts.html#the-raise-statement
        # https://stackoverflow.com/a/24752607
        # __tracebackhide__: bool = True
        raise err from None

    async def result(self) -> Any | Exception:
        '''
        From some (caller) side task, wait for and return the final
        result from the remote (callee) side's task.

        This provides a mechanism for one task running in some actor to wait
        on another task at the other side, in some other actor, to terminate.

        If the remote task is still in a streaming state (it is delivering
        values from inside a ``Context.open_stream():`` block, then those
        msgs are drained but discarded since it is presumed this side of
        the context has already finished with its own streaming logic.

        If the remote context (or its containing actor runtime) was
        canceled, either by a local task calling one of
        ``Context.cancel()`` or `Portal.cancel_actor()``, we ignore the
        received ``ContextCancelled`` exception if the context or
        underlying IPC channel is marked as having been "cancel called".
        This is similar behavior to using ``trio.Nursery.cancel()``
        wherein tasks which raise ``trio.Cancel`` are silently reaped;
        the main different in this API is in the "cancel called" case,
        instead of just not raising, we also return the exception *as
        the result* since client code may be interested in the details
        of the remote cancellation.

        '''
        assert self._portal, "Context.result() can not be called from callee!"
        assert self._recv_chan

        raise_overrun: bool = not self._allow_overruns
        # if re := self._remote_error:
        #     return self._maybe_raise_remote_err(
        #         re,
        #         # NOTE: obvi we don't care if we
        #         # overran the far end if we're already
        #         # waiting on a final result (msg).
        #         raise_overrun_from_self=raise_overrun,
        #     )

        res_placeholder: int = id(self)
        if (
            self._result == res_placeholder
            and not self._remote_error
            and not self._recv_chan._closed  # type: ignore
        ):

            # wait for a final context result by collecting (but
            # basically ignoring) any bi-dir-stream msgs still in transit
            # from the far end.
            drained_msgs: list[dict] = await _drain_to_final_msg(ctx=self)
            log.runtime(
                'Ctx drained pre-result msgs:\n'
                f'{drained_msgs}'
            )

            # TODO: implement via helper func ^^^^
            # pre_result_drained: list[dict] = []
            # while not self._remote_error:
            #     try:
            #         # NOTE: this REPL usage actually works here dawg! Bo
            #         # from .devx._debug import pause
            #         # await pause()
            #         # if re := self._remote_error:
            #         #     self._maybe_raise_remote_err(
            #         #         re,
            #         #         # NOTE: obvi we don't care if we
            #         #         # overran the far end if we're already
            #         #         # waiting on a final result (msg).
            #         #         raise_overrun_from_self=raise_overrun,
            #         #     )

            #         # TODO: bad idea?
            #         # with trio.CancelScope() as res_cs:
            #         #     self._res_scope = res_cs
            #         #     msg: dict = await self._recv_chan.receive()
            #         # if res_cs.cancelled_caught:

            #         # from .devx._debug import pause
            #         # await pause()
            #         msg: dict = await self._recv_chan.receive()
            #         self._result: Any = msg['return']
            #         log.runtime(
            #             'Context delivered final result msg:\n'
            #             f'{pformat(msg)}'
            #         )
            #         # NOTE: we don't need to do this right?
            #         # XXX: only close the rx mem chan AFTER
            #         # a final result is retreived.
            #         # if self._recv_chan:
            #         #     await self._recv_chan.aclose()
            #         break

            #     # NOTE: we get here if the far end was
            #     # `ContextCancelled` in 2 cases:
            #     # 1. we requested the cancellation and thus
            #     #    SHOULD NOT raise that far end error,
            #     # 2. WE DID NOT REQUEST that cancel and thus
            #     #    SHOULD RAISE HERE!
            #     except trio.Cancelled:

            #         # CASE 2: mask the local cancelled-error(s)
            #         # only when we are sure the remote error is
            #         # the source cause of this local task's
            #         # cancellation.
            #         if re := self._remote_error:
            #             self._maybe_raise_remote_err(re)

            #         # CASE 1: we DID request the cancel we simply
            #         # continue to bubble up as normal.
            #         raise

            #     except KeyError:

            #         if 'yield' in msg:
            #             # far end task is still streaming to us so discard
            #             log.warning(f'Discarding std "yield"\n{msg}')
            #             pre_result_drained.append(msg)
            #             continue

            #         # TODO: work out edge cases here where
            #         # a stream is open but the task also calls
            #         # this?
            #         # -[ ] should be a runtime error if a stream is open
            #         #   right?
            #         elif 'stop' in msg:
            #             log.cancel(
            #                 'Remote stream terminated due to "stop" msg:\n'
            #                 f'{msg}'
            #             )
            #             pre_result_drained.append(msg)
            #             continue

            #         # internal error should never get here
            #         assert msg.get('cid'), (
            #             "Received internal error at portal?"
            #         )

            #         # XXX fallthrough to handle expected error XXX
            #         re: Exception|None = self._remote_error
            #         if re:
            #             log.critical(
            #                 'Remote ctx terminated due to "error" msg:\n'
            #                 f'{re}'
            #             )
            #             assert msg is self._cancel_msg
            #             # NOTE: this solved a super dupe edge case XD
            #             # this was THE super duper edge case of:
            #             # - local task opens a remote task,
            #             # - requests remote cancellation of far end
            #             #   ctx/tasks,
            #             # - needs to wait for the cancel ack msg
            #             #   (ctxc) or some result in the race case
            #             #   where the other side's task returns
            #             #   before the cancel request msg is ever
            #             #   rxed and processed,
            #             # - here this surrounding drain loop (which
            #             #   iterates all ipc msgs until the ack or
            #             #   an early result arrives) was NOT exiting
            #             #   since we are the edge case: local task
            #             #   does not re-raise any ctxc it receives
            #             #   IFF **it** was the cancellation
            #             #   requester..
            #             # will raise if necessary, ow break from
            #             # loop presuming any error terminates the
            #             # context!
            #             self._maybe_raise_remote_err(
            #                 re,
            #                 # NOTE: obvi we don't care if we
            #                 # overran the far end if we're already
            #                 # waiting on a final result (msg).
            #                 # raise_overrun_from_self=False,
            #                 raise_overrun_from_self=raise_overrun,
            #             )

            #             break  # OOOOOF, yeah obvi we need this..

            #         # XXX we should never really get here
            #         # right! since `._deliver_msg()` should
            #         # always have detected an {'error': ..}
            #         # msg and already called this right!?!
            #         elif error := unpack_error(
            #             msg=msg,
            #             chan=self._portal.channel,
            #             hide_tb=False,
            #         ):
            #             log.critical('SHOULD NEVER GET HERE!?')
            #             assert msg is self._cancel_msg
            #             assert error.msgdata == self._remote_error.msgdata
            #             from .devx._debug import pause
            #             await pause()
            #             self._maybe_cancel_and_set_remote_error(error)
            #             self._maybe_raise_remote_err(error)

            #         else:
            #             # bubble the original src key error
            #             raise

        if (
            (re := self._remote_error)
            and self._result == res_placeholder
        ):
            maybe_err: Exception|None = self._maybe_raise_remote_err(
                re,
                # NOTE: obvi we don't care if we
                # overran the far end if we're already
                # waiting on a final result (msg).
                # raise_overrun_from_self=False,
                raise_overrun_from_self=(
                    raise_overrun
                    and
                    # only when we ARE NOT the canceller
                    # should we raise overruns, bc ow we're
                    # raising something we know might happen
                    # during cancellation ;)
                    (not self._cancel_called)
                ),
            )
            if maybe_err:
                self._result = maybe_err

        return self._result

    async def started(
        self,
        value: Any | None = None

    ) -> None:
        '''
        Indicate to calling actor's task that this linked context
        has started and send ``value`` to the other side via IPC.

        On the calling side ``value`` is the second item delivered
        in the tuple returned by ``Portal.open_context()``.

        '''
        if self._portal:
            raise RuntimeError(
                f'Caller side context {self} can not call started!'
            )

        elif self._started_called:
            raise RuntimeError(
                f'called `.started()` twice on context with {self.chan.uid}'
            )

        await self.chan.send({'started': value, 'cid': self.cid})
        self._started_called = True

    async def _drain_overflows(
        self,
    ) -> None:
        '''
        Private task spawned to push newly received msgs to the local
        task which getting overrun by the remote side.

        In order to not block the rpc msg loop, but also not discard
        msgs received in this context, we need to async push msgs in
        a new task which only runs for as long as the local task is in
        an overrun state.

        '''
        self._in_overrun = True
        try:
            while self._overflow_q:
                # NOTE: these msgs should never be errors since we always do
                # the check prior to checking if we're in an overrun state
                # inside ``._deliver_msg()``.
                msg = self._overflow_q.popleft()
                try:
                    await self._send_chan.send(msg)
                except trio.BrokenResourceError:
                    log.warning(
                        f"{self._send_chan} consumer is already closed"
                    )
                    return
                except trio.Cancelled:
                    # we are obviously still in overrun
                    # but the context is being closed anyway
                    # so we just warn that there are un received
                    # msgs still..
                    self._overflow_q.appendleft(msg)
                    fmt_msgs = ''
                    for msg in self._overflow_q:
                        fmt_msgs += f'{pformat(msg)}\n'

                    log.warning(
                        f'Context for {self.cid} is being closed while '
                        'in an overrun state!\n'
                        'Discarding the following msgs:\n'
                        f'{fmt_msgs}\n'
                    )
                    raise

        finally:
            # task is now finished with the backlog so mark us as
            # no longer in backlog.
            self._in_overrun = False

    async def _deliver_msg(
        self,
        msg: dict,

    ) -> bool:
        '''
        Deliver an IPC msg received from a transport-channel to
        this context's underlying mem chan for handling by local
        user application tasks; deliver `bool` indicating whether
        the msg was able to be delivered.

        If `._allow_overruns == True` (maybe) append the msg to an
        "overflow queue" and start a "drainer task" (inside the
        `._scope_nursery: trio.Nursery`) which ensures that such
        messages are queued up and eventually sent if possible.

        '''
        cid: str = self.cid
        chan: Channel = self.chan
        from_uid: tuple[str, str]  = chan.uid
        send_chan: trio.MemorySendChannel = self._send_chan
        nsf: NamespacePath = self._nsf

        re: Exception|None
        if re := unpack_error(
            msg,
            self.chan,
        ):
            log.error(
                f'Delivering error-msg to caller\n'
                f'<= peer: {from_uid}\n'
                f'  |_ {nsf}()\n\n'

                f'=> cid: {cid}\n'
                f'  |_{self._task}\n\n'

                f'{pformat(re)}\n'
            )
            self._cancel_msg: dict = msg

            # NOTE: this will not raise an error, merely set
            # `._remote_error` and maybe cancel any task currently
            # entered in `Portal.open_context()` presuming the
            # error is "cancel causing" (i.e. `ContextCancelled`
            # or `RemoteActorError`).
            self._maybe_cancel_and_set_remote_error(re)

            # XXX NEVER do this XXX..!!
            # bc if the error is a ctxc and there is a task
            # waiting on `.result()` we need the msg to be sent
            # over the `send_chan`/`._recv_chan` so that the error
            # is relayed to that waiter task..
            # return True
            #
            # XXX ALSO NO!! XXX
            # if self._remote_error:
            #     self._maybe_raise_remote_err(error)

        if self._in_overrun:
            log.warning(
                f'Queueing OVERRUN msg on caller task:\n'
                f'<= peer: {from_uid}\n'
                f'  |_ {nsf}()\n\n'

                f'=> cid: {cid}\n'
                f'  |_{self._task}\n\n'

                f'{pformat(msg)}\n'
            )
            self._overflow_q.append(msg)
            return False

        try:
            log.runtime(
                f'Delivering msg from IPC ctx:\n'
                f'<= {from_uid}\n'
                f'  |_ {nsf}()\n\n'

                f'=> {self._task}\n'
                f'  |_cid={self.cid}\n\n'

                f'{pformat(msg)}\n'
            )
            # from .devx._debug import pause
            # await pause()

            # NOTE: if an error is deteced we should always still
            # send it through the feeder-mem-chan and expect
            # it to be raised by any context (stream) consumer
            # task via the consumer APIs on both the `Context` and
            # `MsgStream`!
            #
            # XXX the reason is that this method is always called
            # by the IPC msg handling runtime task and that is not
            # normally the task that should get cancelled/error
            # from some remote fault!
            send_chan.send_nowait(msg)
            return True

        except trio.BrokenResourceError:
            # TODO: what is the right way to handle the case where the
            # local task has already sent a 'stop' / StopAsyncInteration
            # to the other side but and possibly has closed the local
            # feeder mem chan? Do we wait for some kind of ack or just
            # let this fail silently and bubble up (currently)?

            # XXX: local consumer has closed their side
            # so cancel the far end streaming task
            log.warning(
                'Rx chan for `Context` alfready closed?\n'
                f'cid: {self.cid}\n'
                'Failed to deliver msg:\n'
                f'send_chan: {send_chan}\n\n'
                f'{pformat(msg)}\n'
            )
            return False

        # NOTE XXX: by default we do **not** maintain context-stream
        # backpressure and instead opt to relay stream overrun errors to
        # the sender; the main motivation is that using bp can block the
        # msg handling loop which calls into this method!
        except trio.WouldBlock:

            # XXX: always push an error even if the local receiver
            # is in overrun state - i.e. if an 'error' msg is
            # delivered then
            # `._maybe_cancel_and_set_remote_error(msg)` should
            # have already been called above!
            #
            # XXX QUESTION XXX: if we rx an error while in an
            # overrun state and that msg isn't stuck in an
            # overflow queue what happens?!?

            local_uid = current_actor().uid
            txt: str = (
                'on IPC context:\n'

                f'<= sender: {from_uid}\n'
                f'  |_ {self._nsf}()\n\n'

                f'=> overrun: {local_uid}\n'
                f'  |_cid: {cid}\n'
                f'  |_task: {self._task}\n'
            )
            if not self._stream_opened:
                txt += (
                    f'\n*** No stream open on `{local_uid[0]}` side! ***\n\n'
                    f'{msg}\n'
                )

            # XXX: lul, this really can't be backpressure since any
            # blocking here will block the entire msg loop rpc sched for
            # a whole channel.. maybe we should rename it?
            if self._allow_overruns:
                txt += (
                    '\n*** Starting overflow queuing task on msg ***\n\n'
                    f'{msg}\n'
                )
                log.warning(txt)
                if (
                    not self._in_overrun
                ):
                    self._overflow_q.append(msg)
                    tn: trio.Nursery = self._scope_nursery
                    assert not tn.child_tasks
                    try:
                        tn.start_soon(
                            self._drain_overflows,
                        )
                        return True

                    except RuntimeError:
                        # if the nursery is already cancelled due to
                        # this context exiting or in error, we ignore
                        # the nursery error since we never expected
                        # anything different.
                        return False
            else:
                txt += f'\n{msg}\n'
                # raise local overrun and immediately pack as IPC
                # msg for far end.
                try:
                    raise StreamOverrun(
                        txt,
                        sender=from_uid,
                    )
                except StreamOverrun as err:
                    err_msg: dict[str, dict] = pack_error(
                        err,
                        cid=cid,
                    )
                    try:
                        # relay condition to sender side remote task
                        await chan.send(err_msg)
                        return True

                    except trio.BrokenResourceError:
                        # XXX: local consumer has closed their side
                        # so cancel the far end streaming task
                        log.warning(
                            'Channel for ctx is already closed?\n'
                            f'|_{chan}\n'
                        )

            # ow, indicate unable to deliver by default
            return False


def mk_context(
    chan: Channel,
    cid: str,
    nsf: NamespacePath,

    msg_buffer_size: int = 2**6,

    **kwargs,

) -> Context:
    '''
    Internal factory to create an inter-actor task ``Context``.

    This is called by internals and should generally never be called
    by user code.

    '''
    send_chan: trio.MemorySendChannel
    recv_chan: trio.MemoryReceiveChannel
    send_chan, recv_chan = trio.open_memory_channel(msg_buffer_size)

    ctx = Context(
        chan=chan,
        cid=cid,
        _send_chan=send_chan,
        _recv_chan=recv_chan,
        _nsf=nsf,
        _task=trio.lowlevel.current_task(),
        **kwargs,
    )
    ctx._result: int | Any = id(ctx)
    return ctx


def context(func: Callable) -> Callable:
    '''
    Mark an async function as a streaming routine with ``@context``.

    '''
    # TODO: apply whatever solution ``mypy`` ends up picking for this:
    # https://github.com/python/mypy/issues/2087#issuecomment-769266912
    func._tractor_context_function = True  # type: ignore

    sig = inspect.signature(func)
    params = sig.parameters
    if 'ctx' not in params:
        raise TypeError(
            "The first argument to the context function "
            f"{func.__name__} must be `ctx: tractor.Context`"
        )
    return func
