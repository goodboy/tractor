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
from contextlib import asynccontextmanager as acm
import inspect
from pprint import pformat
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
    ContextCancelled,
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
        self._eoc: bool|trio.EndOfChannel = False
        self._closed: bool|trio.ClosedResourceError = False

    # delegate directly to underlying mem channel
    def receive_nowait(
        self,
        allow_msg_keys: list[str] = ['yield'],
    ):
        msg: dict = self._rx_chan.receive_nowait()
        for (
            i,
            key,
        ) in enumerate(allow_msg_keys):
            try:
                return msg[key]
            except KeyError as kerr:
                if i < (len(allow_msg_keys) - 1):
                    continue

                _raise_from_no_key_in_msg(
                    ctx=self._ctx,
                    msg=msg,
                    src_err=kerr,
                    log=log,
                    expect_key=key,
                    stream=self,
                )

    async def receive(
        self,

        hide_tb: bool = True,
    ):
        '''
        Receive a single msg from the IPC transport, the next in
        sequence sent by the far end task (possibly in order as
        determined by the underlying protocol).

        '''
        __tracebackhide__: bool = hide_tb

        # NOTE: `trio.ReceiveChannel` implements
        # EOC handling as follows (aka uses it
        # to gracefully exit async for loops):
        #
        # async def __anext__(self) -> ReceiveType:
        #     try:
        #         return await self.receive()
        #     except trio.EndOfChannel:
        #         raise StopAsyncIteration
        #
        # see ``.aclose()`` for notes on the old behaviour prior to
        # introducing this
        if self._eoc:
            raise self._eoc

        if self._closed:
            raise self._closed

        src_err: Exception|None = None  # orig tb
        try:
            try:
                msg = await self._rx_chan.receive()
                return msg['yield']

            except KeyError as kerr:
                src_err = kerr

                # NOTE: may raise any of the below error types
                # includg EoC when a 'stop' msg is found.
                _raise_from_no_key_in_msg(
                    ctx=self._ctx,
                    msg=msg,
                    src_err=kerr,
                    log=log,
                    expect_key='yield',
                    stream=self,
                )

        # XXX: the stream terminates on either of:
        # - via `self._rx_chan.receive()` raising  after manual closure
        #   by the rpc-runtime OR,
        # - via a received `{'stop': ...}` msg from remote side.
        #   |_ NOTE: previously this was triggered by calling
        #   ``._rx_chan.aclose()`` on the send side of the channel inside
        #   `Actor._push_result()`, but now the 'stop' message handling
        #   has been put just above inside `_raise_from_no_key_in_msg()`.
        except (
            trio.EndOfChannel,
        ) as eoc:
            src_err = eoc
            self._eoc = eoc

            # TODO: Locally, we want to close this stream gracefully, by
            # terminating any local consumers tasks deterministically.
            # Once we have broadcast support, we **don't** want to be
            # closing this stream and not flushing a final value to
            # remaining (clone) consumers who may not have been
            # scheduled to receive it yet.
            # try:
            #     maybe_err_msg_or_res: dict = self._rx_chan.receive_nowait()
            #     if maybe_err_msg_or_res:
            #         log.warning(
            #             'Discarding un-processed msg:\n'
            #             f'{maybe_err_msg_or_res}'
            #         )
            # except trio.WouldBlock:
            #     # no queued msgs that might be another remote
            #     # error, so just raise the original EoC
            #     pass

            # raise eoc

        # a ``ClosedResourceError`` indicates that the internal
        # feeder memory receive channel was closed likely by the
        # runtime after the associated transport-channel
        # disconnected or broke.
        except trio.ClosedResourceError as cre:  # by self._rx_chan.receive()
            src_err = cre
            log.warning(
                '`Context._rx_chan` was already closed?'
            )
            self._closed = cre

        # when the send is closed we assume the stream has
        # terminated and signal this local iterator to stop
        drained: list[Exception|dict] = await self.aclose()
        if drained:
            # from .devx import pause
            # await pause()
            log.warning(
                'Drained context msgs during closure:\n'
                f'{drained}'
            )
        # TODO: pass these to the `._ctx._drained_msgs: deque`
        # and then iterate them as part of any `.result()` call?

        # NOTE XXX: if the context was cancelled or remote-errored
        # but we received the stream close msg first, we
        # probably want to instead raise the remote error
        # over the end-of-stream connection error since likely
        # the remote error was the source cause?
        ctx: Context = self._ctx
        ctx.maybe_raise(
            raise_ctxc_from_self_call=True,
        )

        # propagate any error but hide low-level frame details
        # from the caller by default for debug noise reduction.
        if (
            hide_tb

            # XXX NOTE XXX don't reraise on certain
            # stream-specific internal error types like,
            #
            # - `trio.EoC` since we want to use the exact instance
            #   to ensure that it is the error that bubbles upward
            #   for silent absorption by `Context.open_stream()`.
            and not self._eoc

            # - `RemoteActorError` (or `ContextCancelled`) if it gets
            #   raised from `_raise_from_no_key_in_msg()` since we
            #   want the same (as the above bullet) for any
            #   `.open_context()` block bubbled error raised by
            #   any nearby ctx API remote-failures.
            # and not isinstance(src_err, RemoteActorError)
        ):
            raise type(src_err)(*src_err.args) from src_err
        else:
            raise src_err

    async def aclose(self) -> list[Exception|dict]:
        '''
        Cancel associated remote actor task and local memory channel on
        close.

        Notes: 
         - REMEMBER that this is also called by `.__aexit__()` so
           careful consideration must be made to handle whatever
           internal stsate is mutated, particuarly in terms of
           draining IPC msgs!

         - more or less we try to maintain adherance to trio's `.aclose()` semantics:
           https://trio.readthedocs.io/en/stable/reference-io.html#trio.abc.AsyncResource.aclose
        '''

        # rx_chan = self._rx_chan

        # XXX NOTE XXX
        # it's SUPER IMPORTANT that we ensure we don't DOUBLE
        # DRAIN msgs on closure so avoid getting stuck handing on
        # the `._rx_chan` since we call this method on
        # `.__aexit__()` as well!!!
        # => SO ENSURE WE CATCH ALL TERMINATION STATES in this
        # block including the EoC..
        if self.closed:
            # this stream has already been closed so silently succeed as
            # per ``trio.AsyncResource`` semantics.
            # https://trio.readthedocs.io/en/stable/reference-io.html#trio.abc.AsyncResource.aclose
            return []

        ctx: Context = self._ctx
        drained: list[Exception|dict] = []
        while not drained:
            try:
                maybe_final_msg = self.receive_nowait(
                    allow_msg_keys=['yield', 'return'],
                )
                if maybe_final_msg:
                    log.debug(
                        'Drained un-processed stream msg:\n'
                        f'{pformat(maybe_final_msg)}'
                    )
                    # TODO: inject into parent `Context` buf?
                    drained.append(maybe_final_msg)

            # NOTE: we only need these handlers due to the
            # `.receive_nowait()` call above which may re-raise
            # one of these errors on a msg key error!

            except trio.WouldBlock as be:
                drained.append(be)
                break

            except trio.EndOfChannel as eoc:
                self._eoc: Exception = eoc
                drained.append(eoc)
                break

            except trio.ClosedResourceError as cre:
                self._closed = cre
                drained.append(cre)
                break

            except ContextCancelled as ctxc:
                # log.exception('GOT CTXC')
                log.cancel(
                    'Context was cancelled during stream closure:\n'
                    f'canceller: {ctxc.canceller}\n'
                    f'{pformat(ctxc.msgdata)}'
                )
                break

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
        ) as re:
            # the underlying channel may already have been pulled
            # in which case our stop message is meaningless since
            # it can't traverse the transport.
            log.warning(
                f'Stream was already destroyed?\n'
                f'actor: {ctx.chan.uid}\n'
                f'ctx id: {ctx.cid}'
            )
            drained.append(re)
            self._closed = re

        # if caught_eoc:
        #     # from .devx import _debug
        #     # await _debug.pause()
        #     with trio.CancelScope(shield=True):
        #         await rx_chan.aclose()

        if not self._eoc:
            log.cancel(
                'Stream closed before it received an EoC?\n'
                'Setting eoc manually..\n..'
            )
            self._eoc: bool = trio.EndOfChannel(
                f'Context stream closed by {self._ctx.side}\n'
                f'|_{self}\n'
            )
        # ?XXX WAIT, why do we not close the local mem chan `._rx_chan` XXX?
        # => NO, DEFINITELY NOT! <=
        # if we're a bi-dir ``MsgStream`` BECAUSE this same
        # core-msg-loop mem recv-chan is used to deliver the
        # potential final result from the surrounding inter-actor
        # `Context` so we don't want to close it until that
        # context has run to completion.

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
        # self._closed = True
        return drained

    @property
    def closed(self) -> bool:

        rxc: bool = self._rx_chan._closed
        _closed: bool|Exception = self._closed
        _eoc: bool|trio.EndOfChannel = self._eoc
        if rxc or _closed or _eoc:
            log.runtime(
                f'`MsgStream` is already closed\n'
                f'{self}\n'
                f' |_cid: {self._ctx.cid}\n'
                f' |_rx_chan._closed: {type(rxc)} = {rxc}\n'
                f' |_closed: {type(_closed)} = {_closed}\n'
                f' |_eoc: {type(_eoc)} = {_eoc}'
            )
            return True
        return False

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
        data: Any,

        hide_tb: bool = True,
    ) -> None:
        '''
        Send a message over this stream to the far end.

        '''
        __tracebackhide__: bool = hide_tb

        # raise any alreay known error immediately
        self._ctx.maybe_raise()
        if self._eoc:
            raise self._eoc

        if self._closed:
            raise self._closed

        try:
            await self._ctx.chan.send(
                payload={
                    'yield': data,
                    'cid': self._ctx.cid,
                },
                # hide_tb=hide_tb,
            )
        except (
            trio.ClosedResourceError,
            trio.BrokenResourceError,
            BrokenPipeError,
        ) as trans_err:
            if hide_tb:
                raise type(trans_err)(
                    *trans_err.args
                ) from trans_err
            else:
                raise


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
