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

from ._exceptions import (
    unpack_error,
    pack_error,
    ContextCancelled,
    StreamOverrun,
)
from .log import get_logger
from ._ipc import Channel
from ._streaming import MsgStream
from ._state import current_actor

if TYPE_CHECKING:
    from ._portal import Portal


log = get_logger(__name__)


@dataclass
class Context:
    '''
    An inter-actor, ``trio``-task communication context.

    NB: This class should never be instatiated directly, it is delivered
    by either,
     - runtime machinery to a remotely started task or,
     - by entering ``Portal.open_context()``.

     and is always constructed using ``mkt_context()``.

    Allows maintaining task or protocol specific state between
    2 communicating, parallel executing actor tasks. A unique context is
    allocated on each side of any task RPC-linked msg dialog, for
    every request to a remote actor from a portal. On the "callee"
    side a context is always allocated inside ``._runtime._invoke()``.

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
    _result: Any | int = None
    _remote_error: BaseException | None = None

    # cancellation state
    _cancel_called: bool = False  # did WE cancel the far end?
    _cancelled_remote: tuple[str, str] | None = None

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
    def cancel_called_remote(self) -> tuple[str, str] | None:
        '''
        ``Actor.uid`` of the remote actor who's task was cancelled
        causing this side of the context to also be cancelled.

        '''
        remote_uid = self._cancelled_remote
        if remote_uid:
            return tuple(remote_uid)

    @property
    def cancelled_caught(self) -> bool:
        return self._scope.cancelled_caught

    # init and streaming state
    _started_called: bool = False
    _started_received: bool = False
    _stream_opened: bool = False

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
        await self.chan.send({'stop': True, 'cid': self.cid})

    async def _maybe_cancel_and_set_remote_error(
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
        self._remote_error: BaseException = error

        # always record the remote actor's uid since its cancellation
        # state is directly linked to ours (the local one).
        self._cancelled_remote = self.chan.uid

        if (
            isinstance(error, ContextCancelled)
        ):
            log.cancel(
                'Remote task-context sucessfully cancelled for '
                f'{self.chan.uid}:{self.cid}'
            )

            if self._cancel_called:
                # from ._debug import breakpoint
                # await breakpoint()

                # this is an expected cancel request response message
                # and we **don't need to raise it** in local cancel
                # scope since it will potentially override a real error.
                return
        else:
            log.error(
                f'Remote context error for {self.chan.uid}:{self.cid}:\n'
                f'{error}'
            )
        # TODO: tempted to **not** do this by-reraising in a
        # nursery and instead cancel a surrounding scope, detect
        # the cancellation, then lookup the error that was set?
        # YES! this is way better and simpler!
        if self._scope:
            # from trio.testing import wait_all_tasks_blocked
            # await wait_all_tasks_blocked()
            # self._cancelled_remote = self.chan.uid
            self._scope.cancel()

            # this REPL usage actually works here BD
            # from .devx._debug import pause
            # await pause()

    async def cancel(
        self,
        timeout: float = 0.616,

    ) -> None:
        '''
        Cancel this inter-actor-task context.

        Request that the far side cancel it's current linked context,
        Timeout quickly in an attempt to sidestep 2-generals...

        '''
        side: str = 'caller' if self._portal else 'callee'
        log.cancel(
            f'Cancelling {side} side of context to {self.chan.uid}'
        )

        self._cancel_called: bool = True

        if side == 'caller':
            if not self._portal:
                raise RuntimeError(
                    "No portal found, this is likely a callee side context"
                )

            cid: str = self.cid
            with trio.move_on_after(timeout) as cs:
                cs.shield = True
                log.cancel(
                    f'Cancelling stream {cid} to '
                    f'{self._portal.channel.uid}'
                )

                # NOTE: we're telling the far end actor to cancel a task
                # corresponding to *this actor*. The far end local channel
                # instance is passed to `Actor._cancel_task()` implicitly.
                await self._portal.run_from_ns(
                    'self',
                    '_cancel_task',
                    cid=cid,
                )
                # print("EXITING CANCEL CALL")

            if cs.cancelled_caught:
                # XXX: there's no way to know if the remote task was indeed
                # cancelled in the case where the connection is broken or
                # some other network error occurred.
                # if not self._portal.channel.connected():
                if not self.chan.connected():
                    log.cancel(
                        'May have failed to cancel remote task '
                        f'{cid} for {self._portal.channel.uid}'
                    )
                else:
                    log.cancel(
                        'Timed out on cancel request of remote task '
                        f'{cid} for {self._portal.channel.uid}'
                    )

        # callee side remote task
        else:
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
        actor = current_actor()

        # here we create a mem chan that corresponds to the
        # far end caller / callee.

        # Likewise if the surrounding context has been cancelled we error here
        # since it likely means the surrounding block was exited or
        # killed

        if self._cancel_called:
            task = trio.lowlevel.current_task().name
            raise ContextCancelled(
                f'Context around {actor.uid[0]}:{task} was already cancelled!'
            )

        if not self._portal and not self._started_called:
            raise RuntimeError(
                'Context.started()` must be called before opening a stream'
            )

        # NOTE: in one way streaming this only happens on the
        # caller side inside `Actor.start_remote_task()` so if you try
        # to send a stop from the caller to the callee in the
        # single-direction-stream case you'll get a lookup error
        # currently.
        ctx = actor.get_context(
            self.chan,
            self.cid,
            msg_buffer_size=msg_buffer_size,
            allow_overruns=allow_overruns,
        )
        ctx._allow_overruns = allow_overruns
        assert ctx is self

        # XXX: If the underlying channel feeder receive mem chan has
        # been closed then likely client code has already exited
        # a ``.open_stream()`` block prior or there was some other
        # unanticipated error or cancellation from ``trio``.

        if ctx._recv_chan._closed:
            raise trio.ClosedResourceError(
                'The underlying channel for this stream was already closed!?')

        async with MsgStream(
            ctx=self,
            rx_chan=ctx._recv_chan,
        ) as stream:

            if self._portal:
                self._portal._streams.add(stream)

            try:
                self._stream_opened = True

                # XXX: do we need this?
                # ensure we aren't cancelled before yielding the stream
                # await trio.lowlevel.checkpoint()
                yield stream

                # NOTE: Make the stream "one-shot use".  On exit, signal
                # ``trio.EndOfChannel``/``StopAsyncIteration`` to the
                # far end.
                await stream.aclose()

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
    ) -> None:
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
        if (
            isinstance(err, ContextCancelled)
            and (
                self._cancel_called
                or self.chan._cancel_called
                or tuple(err.canceller) == current_actor().uid
            )
        ):
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
        __tracebackhide__: bool = True
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
        __tracebackhide__: bool = True
        assert self._portal, "Context.result() can not be called from callee!"
        assert self._recv_chan

        if re := self._remote_error:
            return self._maybe_raise_remote_err(re)

        if (
            self._result == id(self)
            and not self._remote_error
            and not self._recv_chan._closed  # type: ignore
        ):
            # wait for a final context result consuming
            # and discarding any bi dir stream msgs still
            # in transit from the far end.
            while True:
                try:
                    msg = await self._recv_chan.receive()
                    self._result: Any = msg['return']

                    # NOTE: we don't need to do this right?
                    # XXX: only close the rx mem chan AFTER
                    # a final result is retreived.
                    # if self._recv_chan:
                    #     await self._recv_chan.aclose()

                    break

                # NOTE: we get here if the far end was
                # `ContextCancelled` in 2 cases:
                # 1. we requested the cancellation and thus
                #    SHOULD NOT raise that far end error,
                # 2. WE DID NOT REQUEST that cancel and thus
                #    SHOULD RAISE HERE!
                except trio.Cancelled:

                    # CASE 2: mask the local cancelled-error(s)
                    # only when we are sure the remote error is the
                    # (likely) source cause of this local runtime
                    # task's cancellation.
                    if re := self._remote_error:
                        self._maybe_raise_remote_err(re)

                    # CASE 1: we DID request the cancel we simply
                    # continue to bubble up as normal.
                    raise

                except KeyError:  # as msgerr:

                    if 'yield' in msg:
                        # far end task is still streaming to us so discard
                        log.warning(f'Discarding stream delivered {msg}')
                        continue

                    elif 'stop' in msg:
                        log.debug('Remote stream terminated')
                        continue

                    # internal error should never get here
                    assert msg.get('cid'), (
                        "Received internal error at portal?"
                    )

                    err = unpack_error(
                        msg,
                        self._portal.channel
                    )  # from msgerr

                    err = self._maybe_raise_remote_err(err)
                    self._remote_error = err

        if re := self._remote_error:
            return self._maybe_raise_remote_err(re)

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
                # inside ``.deliver_msg()``.
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

        # draining: bool = False,

    ) -> bool:
        '''
        Deliver an IPC msg received from a transport-channel to
        this context's underlying mem chan for handling by
        user operating tasks; deliver a bool indicating whether the
        msg was immediately sent.

        If `._allow_overruns == True` (maybe) append the msg to an
        "overflow queue" and start a "drainer task" (inside the
        `._scope_nursery: trio.Nursery`) which ensures that such
        messages are eventually sent if possible.

        '''
        cid = self.cid
        chan = self.chan
        uid = chan.uid
        send_chan: trio.MemorySendChannel = self._send_chan

        log.runtime(
            f"Delivering {msg} from {uid} to caller {cid}"
        )

        error = msg.get('error')
        if error := unpack_error(
                msg,
                self.chan,
        ):
            self._cancel_msg = msg
            await self._maybe_cancel_and_set_remote_error(error)

        if (
            self._in_overrun
        ):
            self._overflow_q.append(msg)
            return False

        try:
            send_chan.send_nowait(msg)
            return True
            # if an error is deteced we should always
            # expect it to be raised by any context (stream)
            # consumer task

        except trio.BrokenResourceError:
            # TODO: what is the right way to handle the case where the
            # local task has already sent a 'stop' / StopAsyncInteration
            # to the other side but and possibly has closed the local
            # feeder mem chan? Do we wait for some kind of ack or just
            # let this fail silently and bubble up (currently)?

            # XXX: local consumer has closed their side
            # so cancel the far end streaming task
            log.warning(f"{send_chan} consumer is already closed")
            return False

        # NOTE XXX: by default we do **not** maintain context-stream
        # backpressure and instead opt to relay stream overrun errors to
        # the sender; the main motivation is that using bp can block the
        # msg handling loop which calls into this method!
        except trio.WouldBlock:

            # XXX: always push an error even if the local
            # receiver is in overrun state.
            # await self._maybe_cancel_and_set_remote_error(msg)

            local_uid = current_actor().uid
            lines = [
                f'OVERRUN on actor-task context {cid}@{local_uid}!\n'
                # TODO: put remote task name here if possible?
                f'remote sender actor: {uid}',
                # TODO: put task func name here and maybe an arrow
                # from sender to overrunner?
                # f'local task {self.func_name}'
            ]
            if not self._stream_opened:
                lines.insert(
                    1,
                    f'\n*** No stream open on `{local_uid[0]}` side! ***\n'
                )

            text = '\n'.join(lines)

            # XXX: lul, this really can't be backpressure since any
            # blocking here will block the entire msg loop rpc sched for
            # a whole channel.. maybe we should rename it?
            if self._allow_overruns:
                text += f'\nStarting overflow queuing task on msg: {msg}'
                log.warning(text)
                if (
                    not self._in_overrun
                ):
                    self._overflow_q.append(msg)
                    n = self._scope_nursery
                    assert not n.child_tasks
                    try:
                        n.start_soon(
                            self._drain_overflows,
                        )
                    except RuntimeError:
                        # if the nursery is already cancelled due to
                        # this context exiting or in error, we ignore
                        # the nursery error since we never expected
                        # anything different.
                        return False
            else:
                try:
                    raise StreamOverrun(text)
                except StreamOverrun as err:
                    err_msg = pack_error(err)
                    err_msg['cid'] = cid
                    try:
                        await chan.send(err_msg)
                    except trio.BrokenResourceError:
                        # XXX: local consumer has closed their side
                        # so cancel the far end streaming task
                        log.warning(f"{chan} is already closed")

            return False


def mk_context(
    chan: Channel,
    cid: str,
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
        chan,
        cid,
        _send_chan=send_chan,
        _recv_chan=recv_chan,
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
