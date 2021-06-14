"""
Message stream types and APIs.

"""
import inspect
from contextlib import contextmanager, asynccontextmanager
from dataclasses import dataclass
from typing import (
    Any, Iterator, Optional, Callable,
    AsyncGenerator,
)

import warnings

import trio

from ._ipc import Channel
from ._exceptions import unpack_error
from ._state import current_actor
from .log import get_logger


log = get_logger(__name__)


# TODO: generic typing like trio's receive channel
# but with msgspec messages?
# class ReceiveChannel(AsyncResource, Generic[ReceiveType]):


class ReceiveMsgStream(trio.abc.ReceiveChannel):
    """A wrapper around a ``trio._channel.MemoryReceiveChannel`` with
    special behaviour for signalling stream termination across an
    inter-actor ``Channel``. This is the type returned to a local task
    which invoked a remote streaming function using `Portal.run()`.

    Termination rules:

    - if the local task signals stop iteration a cancel signal is
      relayed to the remote task indicating to stop streaming
    - if the remote task signals the end of a stream, raise
      a ``StopAsyncIteration`` to terminate the local ``async for``

    """
    def __init__(
        self,
        ctx: 'Context',  # typing: ignore # noqa
        rx_chan: trio.abc.ReceiveChannel,
        shield: bool = False,
    ) -> None:
        self._ctx = ctx
        self._rx_chan = rx_chan
        self._shielded = shield

        # flag to denote end of stream
        self._eoc: bool = False

    # delegate directly to underlying mem channel
    def receive_nowait(self):
        msg = self._rx_chan.receive_nowait()
        return msg['yield']

    async def receive(self):
        # see ``.aclose()`` for an alt to always checking this
        if self._eoc:
            raise trio.EndOfChannel

        try:
            msg = await self._rx_chan.receive()
            return msg['yield']

        except KeyError:
            # internal error should never get here
            assert msg.get('cid'), ("Received internal error at portal?")

            # TODO: handle 2 cases with 3.10 match syntax
            # - 'stop'
            # - 'error'
            # possibly just handle msg['stop'] here!

            if msg.get('stop'):
                log.debug(f"{self} was stopped at remote end")

                # when the send is closed we assume the stream has
                # terminated and signal this local iterator to stop
                await self.aclose()

                # XXX: this causes ``ReceiveChannel.__anext__()`` to
                # raise a ``StopAsyncIteration``.
                raise trio.EndOfChannel

            # TODO: test that shows stream raising an expected error!!!
            elif msg.get('error'):
                # raise the error message
                raise unpack_error(msg, self._ctx.chan)

            else:
                raise

        except trio.ClosedResourceError:
            # XXX: this indicates that a `stop` message was
            # sent by the far side of the underlying channel.
            # Currently this is triggered by calling ``.aclose()`` on
            # the send side of the channel inside
            # ``Actor._push_result()``, but maybe it should be put here?
            # to avoid exposing the internal mem chan closing mechanism?
            # in theory we could instead do some flushing of the channel
            # if needed to ensure all consumers are complete before
            # triggering closure too early?

            # Locally, we want to close this stream gracefully, by
            # terminating any local consumers tasks deterministically.
            # We **don't** want to be closing this send channel and not
            # relaying a final value to remaining consumers who may not
            # have been scheduled to receive it yet?

            # lots of testing to do here

            # when the send is closed we assume the stream has
            # terminated and signal this local iterator to stop
            await self.aclose()

            raise  # propagate

        except trio.Cancelled:
            # relay cancels to the remote task
            await self.aclose()
            raise

    @contextmanager
    def shield(
        self
    ) -> Iterator['ReceiveMsgStream']:  # noqa
        """Shield this stream's underlying channel such that a local consumer task
        can be cancelled (and possibly restarted) using ``trio.Cancelled``.

        Note that here, "shielding" here guards against relaying
        a ``'stop'`` message to the far end of the stream thus keeping
        the stream machinery active and ready for further use, it does
        not have anything to do with an internal ``trio.CancelScope``.

        """
        self._shielded = True
        yield self
        self._shielded = False

    async def aclose(self):
        """Cancel associated remote actor task and local memory channel
        on close.

        """
        self._eoc = True

        # XXX: keep proper adherance to trio's `.aclose()` semantics:
        # https://trio.readthedocs.io/en/stable/reference-io.html#trio.abc.AsyncResource.aclose
        rx_chan = self._rx_chan

        if rx_chan._closed:
            log.warning(f"{self} is already closed")

            # this stream has already been closed so silently succeed as
            # per ``trio.AsyncResource`` semantics.
            # https://trio.readthedocs.io/en/stable/reference-io.html#trio.abc.AsyncResource.aclose
            return

        # TODO: broadcasting to multiple consumers
        # stats = rx_chan.statistics()
        # if stats.open_receive_channels > 1:
        #     # if we've been cloned don't kill the stream
        #     log.debug(
        #       "there are still consumers running keeping stream alive")
        #     return

        if self._shielded:
            log.warning(f"{self} is shielded, portal channel being kept alive")
            return

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
        # side and can relay a 'stop'. In the bidirectional case,
        # `Context.open_stream()` will create the `Actor._cids2qs`
        # entry from a call to `Actor.get_memchans()`.
        if not self._ctx._portal:
            try:
                # only for 2 way streams can we can send
                # stop from the caller side
                await self._ctx.send_stop()

            except trio.BrokenResourceError:
                # the underlying channel may already have been pulled
                # in which case our stop message is meaningless since
                # it can't traverse the transport.
                log.debug(f'Channel for {self} was already closed')

        # close the local mem chan??!?

        # NOT if we're a ``MsgStream``!
        # BECAUSE this same core-msg-loop mem recv-chan is used to deliver
        # the potential final result from the surrounding inter-actor
        # `Context` so we don't want to close it until that context has
        # run to completion.

        # XXX: Notes on old behaviour.

        # In the receive-only case, ``Portal.open_stream_from()`` should
        # call this explicitly on teardown but additionally if for some
        # reason stream consumer code tries to manually receive a new
        # value before ``.aclose()`` is called **but** the far end has
        # stopped `.receive()` **must** raise ``trio.EndofChannel`` in
        # order to avoid an infinite hang on ``.__anext__()``. So we can
        # instead uncomment this check and close the underlying msg-loop
        # mem chan below and not then **not** check for ``self._eoc`` in
        # ``.receive()`` (if for some reason we think that check is
        # a bottle neck - not likely) such that the
        # ``trio.ClosedResourceError`` would instead trigger the
        # ``trio.EndOfChannel`` in ``.receive()`` (as it originally was
        # before bi-dir streaming support).

        # if not isinstance(self, MsgStream):
        #     await rx_chan.aclose()

    # TODO: but make it broadcasting to consumers
    # def clone(self):
    #     """Clone this receive channel allowing for multi-task
    #     consumption from the same channel.

    #     """
    #     return ReceiveStream(
    #         self._cid,
    #         self._rx_chan.clone(),
    #         self._portal,
    #     )


class MsgStream(ReceiveMsgStream, trio.abc.Channel):
    """
    Bidirectional message stream for use within an inter-actor actor
    ``Context```.

    """
    async def send(
        self,
        data: Any
    ) -> None:
        await self._ctx.chan.send({'yield': data, 'cid': self._ctx.cid})


@dataclass
class Context:
    '''An inter-actor task communication context.

    Allows maintaining task or protocol specific state between
    2 communicating actor tasks. A unique context is created on the
    callee side/end for every request to a remote actor from a portal.

    A context can be cancelled and (possibly eventually restarted) from
    either side of the underlying IPC channel.

    A context can be used to open task oriented message streams and can
    be thought of as an IPC aware inter-actor cancel scope.

    '''
    chan: Channel
    cid: str

    # only set on the caller side
    _portal: Optional['Portal'] = None    # type: ignore # noqa
    _recv_chan: Optional[trio.MemoryReceiveChannel] = None
    _result: Optional[Any] = False
    _cancel_called: bool = False

    # only set on the callee side
    _cancel_scope: Optional[trio.CancelScope] = None

    async def send_yield(self, data: Any) -> None:

        warnings.warn(
            "`Context.send_yield()` is now deprecated. "
            "Use ``MessageStream.send()``. ",
            DeprecationWarning,
            stacklevel=2,
        )
        await self.chan.send({'yield': data, 'cid': self.cid})

    async def send_stop(self) -> None:
        await self.chan.send({'stop': True, 'cid': self.cid})

    async def cancel(self) -> None:
        '''Cancel this inter-actor-task context.

        Request that the far side cancel it's current linked context,
        Timeout quickly in an attempt to sidestep 2-generals...

        '''
        self._cancel_called = True

        if self._portal:  # caller side:
            if not self._portal:
                raise RuntimeError(
                    "No portal found, this is likely a callee side context"
                )

            cid = self.cid
            with trio.move_on_after(0.5) as cs:
                cs.shield = True
                log.warning(
                    f"Cancelling stream {cid} to "
                    f"{self._portal.channel.uid}")

                # NOTE: we're telling the far end actor to cancel a task
                # corresponding to *this actor*. The far end local channel
                # instance is passed to `Actor._cancel_task()` implicitly.
                await self._portal.run_from_ns('self', '_cancel_task', cid=cid)

            if cs.cancelled_caught:
                # XXX: there's no way to know if the remote task was indeed
                # cancelled in the case where the connection is broken or
                # some other network error occurred.
                # if not self._portal.channel.connected():
                if not self.chan.connected():
                    log.warning(
                        "May have failed to cancel remote task "
                        f"{cid} for {self._portal.channel.uid}")
        else:
            # ensure callee side
            assert self._cancel_scope
            # TODO: should we have an explicit cancel message
            # or is relaying the local `trio.Cancelled` as an
            # {'error': trio.Cancelled, cid: "blah"} enough?
            # This probably gets into the discussion in
            # https://github.com/goodboy/tractor/issues/36
            self._cancel_scope.cancel()

    @asynccontextmanager
    async def open_stream(

        self,
        shield: bool = False,

    ) -> AsyncGenerator[MsgStream, None]:
        '''Open a ``MsgStream``, a bi-directional stream connected to the
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

        # NOTE: in one way streaming this only happens on the
        # caller side inside `Actor.send_cmd()` so if you try
        # to send a stop from the caller to the callee in the
        # single-direction-stream case you'll get a lookup error
        # currently.
        _, recv_chan = actor.get_memchans(
            self.chan.uid,
            self.cid
        )

        # XXX: If the underlying receive mem chan has been closed then
        # likely client code has already exited a ``.open_stream()``
        # block prior. we error here until such a time that we decide
        # allowing streams to be "re-connected" is supported and/or
        # a good idea.
        if recv_chan._closed:
            task = trio.lowlevel.current_task().name
            raise trio.ClosedResourceError(
                f'stream for {actor.uid[0]}:{task} has already been closed.'
                '\nRe-opening a closed stream is not yet supported!'
                '\nConsider re-calling the containing `@tractor.context` func'
            )

        async with MsgStream(
            ctx=self,
            rx_chan=recv_chan,
            shield=shield,
        ) as rchan:

            if self._portal:
                self._portal._streams.add(rchan)

            try:
                # ensure we aren't cancelled before delivering
                # the stream
                # await trio.lowlevel.checkpoint()
                yield rchan

            except trio.EndOfChannel:
                # stream iteration stop signal
                raise

            else:
                # XXX: Make the stream "one-shot use".  On exit, signal
                # ``trio.EndOfChannel``/``StopAsyncIteration`` to the
                # far end.
                await self.send_stop()

            finally:
                if self._portal:
                    self._portal._streams.remove(rchan)

    async def result(self) -> Any:
        '''From a caller side, wait for and return the final result from
        the callee side task.

        '''
        assert self._portal, "Context.result() can not be called from callee!"
        assert self._recv_chan

        if self._result is False:

            if not self._recv_chan._closed:  # type: ignore

                # wait for a final context result consuming
                # and discarding any bi dir stream msgs still
                # in transit from the far end.
                while True:

                    msg = await self._recv_chan.receive()
                    try:
                        self._result = msg['return']
                        break
                    except KeyError:

                        if 'yield' in msg:
                            # far end task is still streaming to us..
                            log.warning(f'Remote stream deliverd {msg}')
                            # do disard
                            continue

                        elif 'stop' in msg:
                            log.debug('Remote stream terminated')
                            continue

                        # internal error should never get here
                        assert msg.get('cid'), (
                            "Received internal error at portal?")
                        raise unpack_error(msg, self._portal.channel)

        return self._result

    async def started(self, value: Optional[Any] = None) -> None:

        if self._portal:
            raise RuntimeError(
                f"Caller side context {self} can not call started!")

        await self.chan.send({'started': value, 'cid': self.cid})

    # TODO: do we need a restart api?
    # async def restart(self) -> None:
    #     pass


def stream(func: Callable) -> Callable:
    """Mark an async function as a streaming routine with ``@stream``.

    """
    # annotate
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
    """Mark an async function as a streaming routine with ``@context``.

    """
    # annotate
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
