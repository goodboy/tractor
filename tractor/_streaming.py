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

"""
Message stream types and APIs.

"""
from __future__ import annotations
import inspect
from contextlib import asynccontextmanager
from collections import deque
from dataclasses import (
    dataclass,
    field,
)
from functools import partial
from pprint import pformat
from typing import (
    Any,
    Optional,
    Callable,
    AsyncGenerator,
    AsyncIterator,
    TYPE_CHECKING,
)
import warnings

import trio

from ._ipc import Channel
from ._exceptions import (
    unpack_error,
    pack_error,
    ContextCancelled,
    StreamOverrun,
)
from .log import get_logger
from ._state import current_actor
from .trionics import (
    broadcast_receiver,
    BroadcastReceiver,
)

if TYPE_CHECKING:
    from ._portal import Portal


log = get_logger(__name__)


# TODO: the list
# - generic typing like trio's receive channel but with msgspec
#   messages? class ReceiveChannel(AsyncResource, Generic[ReceiveType]):
# - use __slots__ on ``Context``?


class MsgStream(trio.abc.Channel):
    '''
    A bidirectional message stream for receiving logically sequenced
    values over an inter-actor IPC ``Channel``.

    This is the type returned to a local task which entered either
    ``Portal.open_stream_from()`` or ``Context.open_stream()``.

    Termination rules:

    - on cancellation the stream is **not** implicitly closed and the
      surrounding ``Context`` is expected to handle how that cancel
      is relayed to any task on the remote side.
    - if the remote task signals the end of a stream the
      ``ReceiveChannel`` semantics dictate that a ``StopAsyncIteration``
      to terminate the local ``async for``.

    '''
    def __init__(
        self,
        ctx: 'Context',  # typing: ignore # noqa
        rx_chan: trio.MemoryReceiveChannel,
        _broadcaster: Optional[BroadcastReceiver] = None,

    ) -> None:
        self._ctx = ctx
        self._rx_chan = rx_chan
        self._broadcaster = _broadcaster

        # flag to denote end of stream
        self._eoc: bool = False
        self._closed: bool = False

    # delegate directly to underlying mem channel
    def receive_nowait(self):
        msg = self._rx_chan.receive_nowait()
        return msg['yield']

    async def receive(self):
        '''Async receive a single msg from the IPC transport, the next
        in sequence for this stream.

        '''
        # see ``.aclose()`` for notes on the old behaviour prior to
        # introducing this
        if self._eoc:
            raise trio.EndOfChannel

        if self._closed:
            raise trio.ClosedResourceError('This stream was closed')

        try:
            msg = await self._rx_chan.receive()
            return msg['yield']

        except KeyError as err:
            # internal error should never get here
            assert msg.get('cid'), ("Received internal error at portal?")

            # TODO: handle 2 cases with 3.10 match syntax
            # - 'stop'
            # - 'error'
            # possibly just handle msg['stop'] here!

            if self._closed:
                raise trio.ClosedResourceError('This stream was closed')

            if msg.get('stop') or self._eoc:
                log.debug(f"{self} was stopped at remote end")

                # XXX: important to set so that a new ``.receive()``
                # call (likely by another task using a broadcast receiver)
                # doesn't accidentally pull the ``return`` message
                # value out of the underlying feed mem chan!
                self._eoc = True

                # # when the send is closed we assume the stream has
                # # terminated and signal this local iterator to stop
                # await self.aclose()

                # XXX: this causes ``ReceiveChannel.__anext__()`` to
                # raise a ``StopAsyncIteration`` **and** in our catch
                # block below it will trigger ``.aclose()``.
                raise trio.EndOfChannel from err

            # TODO: test that shows stream raising an expected error!!!
            elif msg.get('error'):
                # raise the error message
                raise unpack_error(msg, self._ctx.chan)

            else:
                raise

        except (
            trio.ClosedResourceError,  # by self._rx_chan
            trio.EndOfChannel,  # by self._rx_chan or `stop` msg from far end
        ):
            # XXX: we close the stream on any of these error conditions:

            # a ``ClosedResourceError`` indicates that the internal
            # feeder memory receive channel was closed likely by the
            # runtime after the associated transport-channel
            # disconnected or broke.

            # an ``EndOfChannel`` indicates either the internal recv
            # memchan exhausted **or** we raisesd it just above after
            # receiving a `stop` message from the far end of the stream.

            # Previously this was triggered by calling ``.aclose()`` on
            # the send side of the channel inside
            # ``Actor._push_result()`` (should still be commented code
            # there - which should eventually get removed), but now the
            # 'stop' message handling has been put just above.

            # TODO: Locally, we want to close this stream gracefully, by
            # terminating any local consumers tasks deterministically.
            # One we have broadcast support, we **don't** want to be
            # closing this stream and not flushing a final value to
            # remaining (clone) consumers who may not have been
            # scheduled to receive it yet.

            # when the send is closed we assume the stream has
            # terminated and signal this local iterator to stop
            await self.aclose()

            raise  # propagate

    async def aclose(self):
        '''
        Cancel associated remote actor task and local memory channel on
        close.

        '''
        # XXX: keep proper adherance to trio's `.aclose()` semantics:
        # https://trio.readthedocs.io/en/stable/reference-io.html#trio.abc.AsyncResource.aclose
        rx_chan = self._rx_chan

        if rx_chan._closed:
            log.cancel(f"{self} is already closed")

            # this stream has already been closed so silently succeed as
            # per ``trio.AsyncResource`` semantics.
            # https://trio.readthedocs.io/en/stable/reference-io.html#trio.abc.AsyncResource.aclose
            return

        self._eoc = True

        # NOTE: this is super subtle IPC messaging stuff:
        # Relay stop iteration to far end **iff** we're
        # in bidirectional mode. If we're only streaming
        # *from* one side then that side **won't** have an
        # entry in `Actor._cids2qs` (maybe it should though?).
        # So any `yield` or `stop` msgs sent from the caller side
        # will cause key errors on the callee side since there is
        # no entry for a local feeder mem chan since the callee task
        # isn't expecting messages to be sent by the caller.
        # Thus, we must check that this context DOES NOT
        # have a portal reference to ensure this is indeed the callee
        # side and can relay a 'stop'.

        # In the bidirectional case, `Context.open_stream()` will create
        # the `Actor._cids2qs` entry from a call to
        # `Actor.get_context()` and will call us here to send the stop
        # msg in ``__aexit__()`` on teardown.
        try:
            # NOTE: if this call is cancelled we expect this end to
            # handle as though the stop was never sent (though if it
            # was it shouldn't matter since it's unlikely a user
            # will try to re-use a stream after attemping to close
            # it).
            with trio.CancelScope(shield=True):
                await self._ctx.send_stop()

        except (
            trio.BrokenResourceError,
            trio.ClosedResourceError
        ):
            # the underlying channel may already have been pulled
            # in which case our stop message is meaningless since
            # it can't traverse the transport.
            ctx = self._ctx
            log.warning(
                f'Stream was already destroyed?\n'
                f'actor: {ctx.chan.uid}\n'
                f'ctx id: {ctx.cid}'
            )

        self._closed = True

        # Do we close the local mem chan ``self._rx_chan`` ??!?

        # NO, DEFINITELY NOT if we're a bi-dir ``MsgStream``!
        # BECAUSE this same core-msg-loop mem recv-chan is used to deliver
        # the potential final result from the surrounding inter-actor
        # `Context` so we don't want to close it until that context has
        # run to completion.

        # XXX: Notes on old behaviour:
        # await rx_chan.aclose()

        # In the receive-only case, ``Portal.open_stream_from()`` used
        # to rely on this call explicitly on teardown such that a new
        # call to ``.receive()`` after ``rx_chan`` had been closed, would
        # result in us raising a ``trio.EndOfChannel`` (since we
        # remapped the ``trio.ClosedResourceError`). However, now if for some
        # reason the stream's consumer code tries to manually receive a new
        # value before ``.aclose()`` is called **but** the far end has
        # stopped `.receive()` **must** raise ``trio.EndofChannel`` in
        # order to avoid an infinite hang on ``.__anext__()``; this is
        # why we added ``self._eoc`` to denote stream closure indepedent
        # of ``rx_chan``.

        # In theory we could still use this old method and close the
        # underlying msg-loop mem chan as above and then **not** check
        # for ``self._eoc`` in ``.receive()`` (if for some reason we
        # think that check is a bottle neck - not likely) **but** then
        # we would need to map the resulting
        # ``trio.ClosedResourceError`` to a ``trio.EndOfChannel`` in
        # ``.receive()`` (as it originally was before bi-dir streaming
        # support) in order to trigger stream closure. The old behaviour
        # is arguably more confusing since we lose detection of the
        # runtime's closure of ``rx_chan`` in the case where we may
        # still need to consume msgs that are "in transit" from the far
        # end (eg. for ``Context.result()``).

    @asynccontextmanager
    async def subscribe(
        self,

    ) -> AsyncIterator[BroadcastReceiver]:
        '''
        Allocate and return a ``BroadcastReceiver`` which delegates
        to this message stream.

        This allows multiple local tasks to receive each their own copy
        of this message stream.

        This operation is indempotent and and mutates this stream's
        receive machinery to copy and window-length-store each received
        value from the far end via the internally created broudcast
        receiver wrapper.

        '''
        # NOTE: This operation is indempotent and non-reversible, so be
        # sure you can deal with any (theoretical) overhead of the the
        # allocated ``BroadcastReceiver`` before calling this method for
        # the first time.
        if self._broadcaster is None:

            bcast = self._broadcaster = broadcast_receiver(
                self,
                # use memory channel size by default
                self._rx_chan._state.max_buffer_size,  # type: ignore
                receive_afunc=self.receive,
            )

            # NOTE: we override the original stream instance's receive
            # method to now delegate to the broadcaster's ``.receive()``
            # such that new subscribers will be copied received values
            # and this stream doesn't have to expect it's original
            # consumer(s) to get a new broadcast rx handle.
            self.receive = bcast.receive  # type: ignore
            # seems there's no graceful way to type this with ``mypy``?
            # https://github.com/python/mypy/issues/708

        async with self._broadcaster.subscribe() as bstream:
            assert bstream.key != self._broadcaster.key
            assert bstream._recv == self._broadcaster._recv

            # NOTE: we patch on a `.send()` to the bcaster so that the
            # caller can still conduct 2-way streaming using this
            # ``bstream`` handle transparently as though it was the msg
            # stream instance.
            bstream.send = self.send  # type: ignore

            yield bstream

    async def send(
        self,
        data: Any
    ) -> None:
        '''
        Send a message over this stream to the far end.

        '''
        if self._ctx._remote_error:
            raise self._ctx._remote_error  # from None

        if self._closed:
            raise trio.ClosedResourceError('This stream was already closed')

        await self._ctx.chan.send({'yield': data, 'cid': self._ctx.cid})


@dataclass
class Context:
    '''
    An inter-actor, ``trio`` task communication context.

    NB: This class should never be instatiated directly, it is delivered
    by either,
     - runtime machinery to a remotely started task or,
     - by entering ``Portal.open_context()``.

    Allows maintaining task or protocol specific state between
    2 communicating actor tasks. A unique context is created on the
    callee side/end for every request to a remote actor from a portal.

    A context can be cancelled and (possibly eventually restarted) from
    either side of the underlying IPC channel, open task oriented
    message streams and acts as an IPC aware inter-actor-task cancel
    scope.

    '''
    chan: Channel
    cid: str

    # these are the "feeder" channels for delivering
    # message values to the local task from the runtime
    # msg processing loop.
    _recv_chan: trio.MemoryReceiveChannel
    _send_chan: trio.MemorySendChannel

    _remote_func_type: str | None = None

    # only set on the caller side
    _portal: Portal | None = None    # type: ignore # noqa
    _result: Any | int = None
    _remote_error: BaseException | None = None

    # cancellation state
    _cancel_called: bool = False
    _cancel_called_remote: tuple | None = None
    _cancel_msg: str | None = None
    _scope: trio.CancelScope | None = None
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
        remote_uid = self._cancel_called_remote
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
        error_msg: dict[str, Any],

    ) -> None:
        '''
        (Maybe) unpack and raise a msg error into the local scope
        nursery for this context.

        Acts as a form of "relay" for a remote error raised
        in the corresponding remote callee task.

        '''
        # If this is an error message from a context opened by
        # ``Portal.open_context()`` we want to interrupt any ongoing
        # (child) tasks within that context to be notified of the remote
        # error relayed here.
        #
        # The reason we may want to raise the remote error immediately
        # is that there is no guarantee the associated local task(s)
        # will attempt to read from any locally opened stream any time
        # soon.
        #
        # NOTE: this only applies when
        # ``Portal.open_context()`` has been called since it is assumed
        # (currently) that other portal APIs (``Portal.run()``,
        # ``.run_in_actor()``) do their own error checking at the point
        # of the call and result processing.
        error = unpack_error(
            error_msg,
            self.chan,
        )

        # XXX: set the remote side's error so that after we cancel
        # whatever task is the opener of this context it can raise
        # that error as the reason.
        self._remote_error = error

        if (
            isinstance(error, ContextCancelled)
        ):
            log.cancel(
                'Remote task-context sucessfully cancelled for '
                f'{self.chan.uid}:{self.cid}'
            )

            if self._cancel_called:
                # this is an expected cancel request response message
                # and we don't need to raise it in scope since it will
                # potentially override a real error
                return
        else:
            log.error(
                f'Remote context error for {self.chan.uid}:{self.cid}:\n'
                f'{error_msg["error"]["tb_str"]}'
            )
        # TODO: tempted to **not** do this by-reraising in a
        # nursery and instead cancel a surrounding scope, detect
        # the cancellation, then lookup the error that was set?
        # YES! this is way better and simpler!
        if (
            self._scope
        ):
            # from trio.testing import wait_all_tasks_blocked
            # await wait_all_tasks_blocked()
            self._cancel_called_remote = self.chan.uid
            self._scope.cancel()

            # NOTE: this usage actually works here B)
            # from ._debug import breakpoint
            # await breakpoint()


        # XXX: this will break early callee results sending
        # since when `.result()` is finally called, this
        # chan will be closed..
        # if self._recv_chan:
        #     await self._recv_chan.aclose()

    async def cancel(
        self,
        msg: str | None = None,
        timeout: float = 0.5,
        # timeout: float = 1000,

    ) -> None:
        '''
        Cancel this inter-actor-task context.

        Request that the far side cancel it's current linked context,
        Timeout quickly in an attempt to sidestep 2-generals...

        '''
        side = 'caller' if self._portal else 'callee'
        if msg:
            assert side == 'callee', 'Only callee side can provide cancel msg'

        log.cancel(f'Cancelling {side} side of context to {self.chan.uid}')

        self._cancel_called = True
        # await _debug.breakpoint()
        # breakpoint()

        if side == 'caller':
            if not self._portal:
                raise RuntimeError(
                    "No portal found, this is likely a callee side context"
                )

            cid = self.cid
            with trio.move_on_after(timeout) as cs:
                # cs.shield = True
                log.cancel(
                    f"Cancelling stream {cid} to "
                    f"{self._portal.channel.uid}")

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
                        "May have failed to cancel remote task "
                        f"{cid} for {self._portal.channel.uid}")
                else:
                    log.cancel(
                        "Timed out on cancelling remote task "
                        f"{cid} for {self._portal.channel.uid}")

        # callee side remote task
        else:
            self._cancel_msg = msg

            # TODO: should we have an explicit cancel message
            # or is relaying the local `trio.Cancelled` as an
            # {'error': trio.Cancelled, cid: "blah"} enough?
            # This probably gets into the discussion in
            # https://github.com/goodboy/tractor/issues/36
            assert self._scope
            self._scope.cancel()

    @asynccontextmanager
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
        # NOTE: whenever the context's "opener" side (task) **is**
        # the side which requested the cancellation (likekly via
        # ``Context.cancel()``), we don't want to re-raise that
        # cancellation signal locally (would be akin to
        # a ``trio.Nursery`` nursery raising ``trio.Cancelled``
        # whenever  ``CancelScope.cancel()`` was called) and instead
        # silently reap the expected cancellation "error"-msg.
        # if 'pikerd' in err.msgdata['tb_str']:
        #     # from . import _debug
        #     # await _debug.breakpoint()
        #     breakpoint()

        if (
            isinstance(err, ContextCancelled)
            and (
                self._cancel_called
                or self.chan._cancel_called
                or tuple(err.canceller) == current_actor().uid
            )
        ):
            return err

        raise err  # from None

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

        # from . import _debug
        # await _debug.breakpoint()

        re = self._remote_error
        if re:
            self._maybe_raise_remote_err(re)
            return re

        if (
            self._result == id(self)
            and not self._remote_error
            and not self._recv_chan._closed  # type: ignore
        ):
            # wait for a final context result consuming
            # and discarding any bi dir stream msgs still
            # in transit from the far end.
            while True:
                msg = await self._recv_chan.receive()
                try:
                    self._result = msg['return']

                    # NOTE: we don't need to do this right?
                    # XXX: only close the rx mem chan AFTER
                    # a final result is retreived.
                    # if self._recv_chan:
                    #     await self._recv_chan.aclose()

                    break
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
                        "Received internal error at portal?")

                    err = unpack_error(
                        msg,
                        self._portal.channel
                    )  # from msgerr

                    err = self._maybe_raise_remote_err(err)
                    self._remote_err = err

        return self._remote_error or self._result

    async def started(
        self,
        value: Any | None = None

    ) -> None:
        '''
        Indicate to calling actor's task that this linked context
        has started and send ``value`` to the other side.

        On the calling side ``value`` is the second item delivered
        in the tuple returned by ``Portal.open_context()``.

        '''
        if self._portal:
            raise RuntimeError(
                f"Caller side context {self} can not call started!")

        elif self._started_called:
            raise RuntimeError(
                f"called 'started' twice on context with {self.chan.uid}")

        await self.chan.send({'started': value, 'cid': self.cid})
        self._started_called = True

    # TODO: do we need a restart api?
    # async def restart(self) -> None:
    #     pass

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

        draining: bool = False,

    ) -> bool:

        cid = self.cid
        chan = self.chan
        uid = chan.uid
        send_chan: trio.MemorySendChannel = self._send_chan

        log.runtime(
            f"Delivering {msg} from {uid} to caller {cid}"
        )

        error = msg.get('error')
        if error:
            await self._maybe_cancel_and_set_remote_error(msg)

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
                f'Actor-task context {cid}@{local_uid} was overrun by remote!',
                f'sender actor: {uid}',
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
                    if n.child_tasks:
                        from . import _debug
                        await _debug.breakpoint()
                    assert not n.child_tasks
                    n.start_soon(
                        self._drain_overflows,
                    )
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
    ctx._result = id(ctx)
    return ctx


def stream(func: Callable) -> Callable:
    '''
    Mark an async function as a streaming routine with ``@stream``.

    '''
    # TODO: apply whatever solution ``mypy`` ends up picking for this:
    # https://github.com/python/mypy/issues/2087#issuecomment-769266912
    func._tractor_stream_function = True  # type: ignore

    sig = inspect.signature(func)
    params = sig.parameters
    if 'stream' not in params and 'ctx' in params:
        warnings.warn(
            "`@tractor.stream decorated funcs should now declare a `stream` "
            " arg, `ctx` is now designated for use with @tractor.context",
            DeprecationWarning,
            stacklevel=2,
        )

    if (
        'ctx' not in params and
        'to_trio' not in params and
        'stream' not in params
    ):
        raise TypeError(
            "The first argument to the stream function "
            f"{func.__name__} must be `ctx: tractor.Context` "
            "(Or ``to_trio`` if using ``asyncio`` in guest mode)."
        )
    return func


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
