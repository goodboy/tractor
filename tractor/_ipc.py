"""
Inter-process comms abstractions
"""
import typing

import msgpack
import trio
from async_generator import asynccontextmanager

from .log import get_logger
log = get_logger('ipc')


class StreamQueue:
    """Stream wrapped as a queue that delivers ``msgpack`` serialized objects.
    """
    def __init__(self, stream: trio.SocketStream):
        self.stream = stream
        self._agen = self._iter_packets()
        self._laddr = self.stream.socket.getsockname()[:2]
        self._raddr = self.stream.socket.getpeername()[:2]
        self._send_lock = trio.Lock()

    async def _iter_packets(self) -> typing.AsyncGenerator[dict, None]:
        """Yield packets from the underlying stream.
        """
        unpacker = msgpack.Unpacker(raw=False, use_list=False)
        while True:
            try:
                data = await self.stream.receive_some(2**10)
                log.trace(f"received {data}")
            except trio.BrokenStreamError:
                log.error(f"Stream connection {self.raddr} broke")
                return

            if data == b'':
                log.debug(f"Stream connection {self.raddr} was closed")
                return

            unpacker.feed(data)
            for packet in unpacker:
                yield packet

    @property
    def laddr(self) -> (str, int):
        return self._laddr

    @property
    def raddr(self) -> (str, int):
        return self._raddr

    async def put(self, data: typing.Any) -> int:
        async with self._send_lock:
            return await self.stream.send_all(
                msgpack.dumps(data, use_bin_type=True))

    async def get(self) -> typing.Any:
        return await self._agen.asend(None)

    def __aiter__(self):
        return self._agen

    def connected(self) -> bool:
        return self.stream.socket.fileno() != -1


class Channel:
    """A channel to actors in other processes.

    Use this to talk to any micro-service daemon or other client(s) over a
    a transport managed by ``trio``.
    """
    def __init__(
        self,
        destaddr: tuple = None,
        on_reconnect: typing.Coroutine = None,
        auto_reconnect: bool = False,
        stream: trio.SocketStream = None,  # expected to be active
    ) -> None:
        self._recon_seq = on_reconnect
        self._autorecon = auto_reconnect
        self.squeue = StreamQueue(stream) if stream else None
        if self.squeue and destaddr:
            raise ValueError(
                f"A stream was provided with local addr {self.laddr}"
            )
        self._destaddr = destaddr or self.squeue.raddr
        # set after handshake - always uid of far end
        self.uid = None
        self._agen = self._aiter_recv()

    def __repr__(self) -> str:
        if self.squeue:
            return repr(
                self.squeue.stream.socket._sock).replace(
                        "socket.socket", "Channel")
        return object.__repr__(self)

    @property
    def laddr(self) -> (str, int):
        return self.squeue.laddr if self.squeue else (None, None)

    @property
    def raddr(self) -> (str, int):
        return self.squeue.raddr if self.squeue else (None, None)

    async def connect(
        self, destaddr: typing.Tuple[str, int] = None, **kwargs
    ) -> trio.SocketStream:
        if self.connected():
            raise RuntimeError("channel is already connected?")
        destaddr = destaddr or self._destaddr
        stream = await trio.open_tcp_stream(*destaddr, **kwargs)
        self.squeue = StreamQueue(stream)
        return stream

    async def send(self, item: typing.Any) -> None:
        log.trace(f"send `{item}`")
        await self.squeue.put(item)

    async def recv(self) -> typing.Any:
        try:
            return await self.squeue.get()
        except trio.BrokenStreamError:
            if self._autorecon:
                await self._reconnect()
                return await self.recv()

    async def aclose(self) -> None:
        log.debug(f"Closing {self}")
        await self.squeue.stream.aclose()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.aclose(*args)

    def __aiter__(self):
        return self._agen

    async def _reconnect(self) -> None:
        """Handle connection failures by polling until a reconnect can be
        established.
        """
        down = False
        while True:
            try:
                with trio.move_on_after(3) as cancel_scope:
                    await self.connect()
                cancelled = cancel_scope.cancelled_caught
                if cancelled:
                    log.warn(
                        "Reconnect timed out after 3 seconds, retrying...")
                    continue
                else:
                    log.warn("Stream connection re-established!")
                    # run any reconnection sequence
                    on_recon = self._recon_seq
                    if on_recon:
                        await on_recon(self)
                    break
            except (OSError, ConnectionRefusedError):
                if not down:
                    down = True
                    log.warn(
                        f"Connection to {self.raddr} went down, waiting"
                        " for re-establishment")
                await trio.sleep(1)

    async def _aiter_recv(
        self
    ) -> typing.AsyncGenerator[typing.Any, None]:
        """Async iterate items from underlying stream.
        """
        while True:
            try:
                async for item in self.squeue:
                    yield item
                    # sent = yield item
                    # if sent is not None:
                    #     # optimization, passing None through all the
                    #     # time is pointless
                    #     await self.squeue.put(sent)
            except trio.BrokenStreamError:
                if not self._autorecon:
                    raise
            await self.aclose()
            if self._autorecon:  # attempt reconnect
                await self._reconnect()
                continue
            else:
                return

    def connected(self) -> bool:
        return self.squeue.connected() if self.squeue else False


@asynccontextmanager
async def _connect_chan(
    host: str, port: int
) -> typing.AsyncContextManager[Channel]:
    """Create and connect a channel with disconnect on context manager
    teardown.
    """
    chan = Channel((host, port))
    await chan.connect()
    yield chan
    await chan.aclose()
