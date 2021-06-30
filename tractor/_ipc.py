"""
Inter-process comms abstractions

"""
import platform
import typing
from typing import Any, Tuple, Optional
from functools import partial

import msgpack
import trio
from async_generator import asynccontextmanager

from .log import get_logger
from ._exceptions import TransportClosed
log = get_logger(__name__)


_is_windows = platform.system() == 'Windows'

# :eyeroll:
try:
    import msgpack_numpy
    Unpacker = msgpack_numpy.Unpacker
except ImportError:
    # just plain ``msgpack`` requires tweaking key settings
    Unpacker = partial(msgpack.Unpacker, strict_map_key=False)


class MsgpackTCPStream:
    '''A ``trio.SocketStream`` delivering ``msgpack`` formatted data
    using ``msgpack-python``.

    '''
    def __init__(
        self,
        stream: trio.SocketStream,

    ) -> None:

        self.stream = stream
        assert self.stream.socket
        # should both be IP sockets
        lsockname = stream.socket.getsockname()
        assert isinstance(lsockname, tuple)
        self._laddr = lsockname[:2]
        rsockname = stream.socket.getpeername()
        assert isinstance(rsockname, tuple)
        self._raddr = rsockname[:2]

        # start and seed first entry to read loop
        self._agen = self._iter_packets()
        # self._agen.asend(None) is None

        self._send_lock = trio.StrictFIFOLock()

    async def _iter_packets(self) -> typing.AsyncGenerator[dict, None]:
        """Yield packets from the underlying stream.
        """
        unpacker = Unpacker(
            raw=False,
            use_list=False,
        )
        while True:
            try:
                data = await self.stream.receive_some(2**10)

            except trio.BrokenResourceError as err:
                msg = err.args[0]

                # XXX: handle connection-reset-by-peer the same as a EOF.
                # we're currently remapping this since we allow
                # a quick connect then drop for root actors when
                # checking to see if there exists an "arbiter"
                # on the chosen sockaddr (``_root.py:108`` or thereabouts)
                if (
                    # nix
                    '[Errno 104]' in msg or

                    # on windows it seems there are a variety of errors
                    # to handle..
                    _is_windows
                ):
                    raise TransportClosed(
                        f'{self} was broken with {msg}'
                    )

                else:
                    raise

            log.transport(f"received {data}")  # type: ignore

            if data == b'':
                raise TransportClosed(
                    f'transport {self} was already closed prior ro read'
                )

            unpacker.feed(data)
            for packet in unpacker:
                yield packet

    @property
    def laddr(self) -> Tuple[Any, ...]:
        return self._laddr

    @property
    def raddr(self) -> Tuple[Any, ...]:
        return self._raddr

    # XXX: should this instead be called `.sendall()`?
    async def send(self, data: Any) -> None:
        async with self._send_lock:
            return await self.stream.send_all(
                msgpack.dumps(data, use_bin_type=True))

    async def recv(self) -> Any:
        return await self._agen.asend(None)

    def __aiter__(self):
        return self._agen

    def connected(self) -> bool:
        return self.stream.socket.fileno() != -1


class Channel:
    """An inter-process channel for communication between (remote) actors.

    Currently the only supported transport is a ``trio.SocketStream``.
    """
    def __init__(
        self,
        destaddr: Optional[Tuple[str, int]] = None,
        on_reconnect: typing.Callable[..., typing.Awaitable] = None,
        auto_reconnect: bool = False,
        stream: trio.SocketStream = None,  # expected to be active

    ) -> None:
        self._recon_seq = on_reconnect
        self._autorecon = auto_reconnect
        self.msgstream: Optional[MsgpackTCPStream] = MsgpackTCPStream(
            stream) if stream else None
        if self.msgstream and destaddr:
            raise ValueError(
                f"A stream was provided with local addr {self.laddr}"
            )
        self._destaddr = self.msgstream.raddr if self.msgstream else destaddr
        # set after handshake - always uid of far end
        self.uid: Optional[Tuple[str, str]] = None
        # set if far end actor errors internally
        self._exc: Optional[Exception] = None
        self._agen = self._aiter_recv()

        self._closed: bool = False

    def __repr__(self) -> str:
        if self.msgstream:
            return repr(
                self.msgstream.stream.socket._sock).replace(  # type: ignore
                        "socket.socket", "Channel")
        return object.__repr__(self)

    @property
    def laddr(self) -> Optional[Tuple[Any, ...]]:
        return self.msgstream.laddr if self.msgstream else None

    @property
    def raddr(self) -> Optional[Tuple[Any, ...]]:
        return self.msgstream.raddr if self.msgstream else None

    async def connect(

        self,
        destaddr: Tuple[Any, ...] = None,
        **kwargs

    ) -> trio.SocketStream:

        if self.connected():
            raise RuntimeError("channel is already connected?")

        destaddr = destaddr or self._destaddr
        assert isinstance(destaddr, tuple)

        stream = await trio.open_tcp_stream(
            *destaddr,
            **kwargs
        )
        self.msgstream = MsgpackTCPStream(stream)

        log.transport(
            f'Opened channel to peer {self.laddr} -> {self.raddr}'
        )
        return stream

    async def send(self, item: Any) -> None:

        log.transport(f"send `{item}`")  # type: ignore
        assert self.msgstream

        await self.msgstream.send(item)

    async def recv(self) -> Any:
        assert self.msgstream

        try:
            return await self.msgstream.recv()

        except trio.BrokenResourceError:
            if self._autorecon:
                await self._reconnect()
                return await self.recv()

            raise

    async def aclose(self) -> None:

        log.transport(
            f'Closing channel to {self.uid} '
            f'{self.laddr} -> {self.raddr}'
        )
        assert self.msgstream
        await self.msgstream.stream.aclose()
        self._closed = True

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
                    log.transport(
                        "Reconnect timed out after 3 seconds, retrying...")
                    continue
                else:
                    log.transport("Stream connection re-established!")
                    # run any reconnection sequence
                    on_recon = self._recon_seq
                    if on_recon:
                        await on_recon(self)
                    break
            except (OSError, ConnectionRefusedError):
                if not down:
                    down = True
                    log.transport(
                        f"Connection to {self.raddr} went down, waiting"
                        " for re-establishment")
                await trio.sleep(1)

    async def _aiter_recv(
        self
    ) -> typing.AsyncGenerator[Any, None]:
        """Async iterate items from underlying stream.
        """
        assert self.msgstream
        while True:
            try:
                async for item in self.msgstream:
                    yield item
                    # sent = yield item
                    # if sent is not None:
                    #     # optimization, passing None through all the
                    #     # time is pointless
                    #     await self.msgstream.send(sent)
            except trio.BrokenResourceError:

                if not self._autorecon:
                    raise

            await self.aclose()

            if self._autorecon:  # attempt reconnect
                await self._reconnect()
                continue
            else:
                return

    def connected(self) -> bool:
        return self.msgstream.connected() if self.msgstream else False


@asynccontextmanager
async def _connect_chan(
    host: str, port: int
) -> typing.AsyncGenerator[Channel, None]:
    """Create and connect a channel with disconnect on context manager
    teardown.
    """
    chan = Channel((host, port))
    await chan.connect()
    yield chan
    await chan.aclose()
