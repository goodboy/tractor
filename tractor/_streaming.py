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
Message stream types and APIs.

The machinery and types behind ``Context.open_stream()``

'''
from __future__ import annotations
import inspect
from contextlib import asynccontextmanager as acm
from typing import (
    Any,
    Callable,
    AsyncIterator,
    TYPE_CHECKING,
)
import warnings

import trio

from ._exceptions import (
    _raise_from_no_key_in_msg,
)
from .log import get_logger
from .trionics import (
    broadcast_receiver,
    BroadcastReceiver,
)

if TYPE_CHECKING:
    from ._context import Context


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
        ctx: Context,  # typing: ignore # noqa
        rx_chan: trio.MemoryReceiveChannel,
        _broadcaster: BroadcastReceiver | None = None,

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
        try:
            return msg['yield']
        except KeyError as kerr:
            _raise_from_no_key_in_msg(
                ctx=self._ctx,
                msg=msg,
                src_err=kerr,
                log=log,
                expect_key='yield',
                stream=self,
            )

    async def receive(self):
        '''
        Receive a single msg from the IPC transport, the next in
        sequence sent by the far end task (possibly in order as
        determined by the underlying protocol).

        '''
        # NOTE: `trio.ReceiveChannel` implements
        # EOC handling as follows (aka uses it
        # to gracefully exit async for loops):
        #
        # async def __anext__(self) -> ReceiveType:
        #     try:
        #         return await self.receive()
        #     except trio.EndOfChannel:
        #         raise StopAsyncIteration

        # see ``.aclose()`` for notes on the old behaviour prior to
        # introducing this
        if self._eoc:
            raise trio.EndOfChannel

        if self._closed:
            raise trio.ClosedResourceError('This stream was closed')

        try:
            msg = await self._rx_chan.receive()
            return msg['yield']

        except KeyError as kerr:
            _raise_from_no_key_in_msg(
                ctx=self._ctx,
                msg=msg,
                src_err=kerr,
                log=log,
                expect_key='yield',
                stream=self,
            )

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

    @acm
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
