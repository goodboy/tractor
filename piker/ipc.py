"""
Inter-process comms abstractions
"""
from typing import Coroutine

import msgpack
import trio

from .log import get_logger
log = get_logger('broker.core')


class StreamQueue:
    """Stream wrapped as a queue that delivers ``msgpack`` serialized objects.
    """
    def __init__(self, stream):
        self.stream = stream
        self.peer = stream.socket.getpeername()
        self._agen = self._iter_packets()

    async def _iter_packets(self):
        """Yield packets from the underlying stream.
        """
        unpacker = msgpack.Unpacker(raw=False)
        while True:
            try:
                data = await self.stream.receive_some(2**10)
                log.trace(f"Data is {data}")
            except trio.BrokenStreamError:
                log.error(f"Stream connection {self.peer} broke")
                return

            if data == b'':
                log.debug("Stream connection was closed")
                return

            unpacker.feed(data)
            for packet in unpacker:
                yield packet

    async def put(self, data):
        return await self.stream.send_all(
            msgpack.dumps(data, use_bin_type=True))

    async def get(self):
        return await self._agen.asend(None)

    async def __aiter__(self):
        return self._agen


class Client:
    """The most basic client.

    Use this to talk to any micro-service daemon or other client(s) over a
    TCP socket managed by ``trio``.
    """
    def __init__(
        self, sockaddr: tuple,
        on_reconnect: Coroutine,
        auto_reconnect: bool = True,
    ):
        self.sockaddr = sockaddr
        self._recon_seq = on_reconnect
        self._autorecon = auto_reconnect
        self.squeue = None

    async def connect(self, sockaddr: tuple = None, **kwargs):
        sockaddr = sockaddr or self.sockaddr
        stream = await trio.open_tcp_stream(*sockaddr, **kwargs)
        self.squeue = StreamQueue(stream)
        return stream

    async def send(self, item):
        await self.squeue.put(item)

    async def recv(self):
        try:
            return await self.squeue.get()
        except trio.BrokenStreamError as err:
            if self._autorecon:
                await self._reconnect()
                return await self.recv()

    async def aclose(self, *args):
        await self.squeue.stream.aclose()

    async def __aenter__(self):
        await self.connect(self.sockaddr)
        return self

    async def __aexit__(self, *args):
        await self.aclose(*args)

    async def _reconnect(self):
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
                    await self._recon_seq(self)
                    break
            except (OSError, ConnectionRefusedError):
                if not down:
                    down = True
                    log.warn(
                        f"Connection to {self.sockaddr} went down, waiting"
                        " for re-establishment")
                await trio.sleep(1)

    async def aiter_recv(self):
        """Async iterate items from underlying stream.
        """
        while True:
            try:
                async for item in self.squeue:
                    yield item
            except trio.BrokenStreamError as err:
                if not self._autorecon:
                    raise
            if self._autorecon:  # attempt reconnect
                await self._reconnect()
                continue
            else:
                return
