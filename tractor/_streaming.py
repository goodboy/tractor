import inspect
from contextlib import contextmanager  # , asynccontextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional
import warnings

import trio

from ._ipc import Channel
from ._exceptions import unpack_error
from .log import get_logger


log = get_logger(__name__)


@dataclass(frozen=True)
class Context:
    """An IAC (inter-actor communication) context.

    Allows maintaining task or protocol specific state between communicating
    actors. A unique context is created on the receiving end for every request
    to a remote actor.

    A context can be cancelled and (eventually) restarted from
    either side of the underlying IPC channel.

    A context can be used to open task oriented message streams.

    """
    chan: Channel
    cid: str

    # only set on the caller side
    _portal: Optional['Portal'] = None    # type: ignore # noqa

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
        """Cancel this inter-actor-task context.

        Request that the far side cancel it's current linked context,
        timeout quickly to sidestep 2-generals...

        """
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
            if not self._portal.channel.connected():
                log.warning(
                    "May have failed to cancel remote task "
                    f"{cid} for {self._portal.channel.uid}")

    # async def restart(self) -> None:
    #     # TODO
    #     pass

    # @asynccontextmanager
    # async def open_stream(
    #     self,
    # ) -> AsyncContextManager:
    #     # TODO
    #     pass


def stream(func):
    """Mark an async function as a streaming routine with ``@stream``.
    """
    func._tractor_stream_function = True
    sig = inspect.signature(func)
    if 'ctx' not in sig.parameters:
        raise TypeError(
            "The first argument to the stream function "
            f"{func.__name__} must be `ctx: tractor.Context`"
        )
    return func


class ReceiveMsgStream(trio.abc.ReceiveChannel):
    """A wrapper around a ``trio._channel.MemoryReceiveChannel`` with
    special behaviour for signalling stream termination across an
    inter-actor ``Channel``. This is the type returned to a local task
    which invoked a remote streaming function using `Portal.run()`.

    Termination rules:
    - if the local task signals stop iteration a cancel signal is
      relayed to the remote task indicating to stop streaming
    - if the remote task signals the end of a stream, raise a
      ``StopAsyncIteration`` to terminate the local ``async for``

    """
    def __init__(
        self,
        ctx: Context,
        rx_chan: trio.abc.ReceiveChannel,
        portal: 'Portal',  # noqa
    ) -> None:
        self._ctx = ctx
        self._rx_chan = rx_chan
        self._portal = portal
        # self._chan = portal.channel
        self._shielded = False

    # delegate directly to underlying mem channel
    def receive_nowait(self):
        return self._rx_chan.receive_nowait()

    async def receive(self):
        try:
            msg = await self._rx_chan.receive()
            return msg['yield']
            # return msg['yield']

        except KeyError:
            # internal error should never get here
            assert msg.get('cid'), ("Received internal error at portal?")

            # TODO: handle 2 cases with 3.10 match syntax
            # - 'stop'
            # - 'error'
            # possibly just handle msg['stop'] here!

            # TODO: test that shows stream raising an expected error!!!
            if msg.get('error'):
                # raise the error message
                raise unpack_error(msg, self._portal.channel)

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
            raise StopAsyncIteration

        except trio.Cancelled:
            # relay cancels to the remote task
            await self.aclose()
            raise

    @contextmanager
    def shield(
        self
    ) -> Iterator['ReceiveStream']:  # noqa
        """Shield this stream's underlying channel such that a local consumer task
        can be cancelled (and possibly restarted) using ``trio.Cancelled``.

        """
        self._shielded = True
        yield self
        self._shielded = False

    async def aclose(self):
        """Cancel associated remote actor task and local memory channel
        on close.
        """
        rx_chan = self._rx_chan

        if rx_chan._closed:
            log.warning(f"{self} is already closed")
            return

        # stats = rx_chan.statistics()
        # if stats.open_receive_channels > 1:
        #     # if we've been cloned don't kill the stream
        #     log.debug(
        #       "there are still consumers running keeping stream alive")
        #     return

        if self._shielded:
            log.warning(f"{self} is shielded, portal channel being kept alive")
            return

        # close the local mem chan
        rx_chan.close()

        # cancel surrounding IPC context
        await self._ctx.cancel()

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
        await self._chan.send({'yield': data, 'cid': self._cid})
