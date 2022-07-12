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
Inter-process comms abstractions

"""
from __future__ import annotations
import platform
import struct
import typing
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
)
from typing import (
    Any,
    runtime_checkable,
    Optional,
    Protocol,
    Type,
    TypeVar,
)

from tricycle import BufferedReceiveStream
import msgspec
import trio
from async_generator import asynccontextmanager

from .log import get_logger
from ._exceptions import TransportClosed
log = get_logger(__name__)


_is_windows = platform.system() == 'Windows'
log = get_logger(__name__)


def get_stream_addrs(stream: trio.SocketStream) -> tuple:
    # should both be IP sockets
    lsockname = stream.socket.getsockname()
    rsockname = stream.socket.getpeername()
    return (
        tuple(lsockname[:2]),
        tuple(rsockname[:2]),
    )


MsgType = TypeVar("MsgType")

# TODO: consider using a generic def and indexing with our eventual
# msg definition/types?
# - https://docs.python.org/3/library/typing.html#typing.Protocol
# - https://jcristharif.com/msgspec/usage.html#structs


@runtime_checkable
class MsgTransport(Protocol[MsgType]):

    stream: trio.SocketStream
    drained: list[MsgType]

    def __init__(self, stream: trio.SocketStream) -> None:
        ...

    # XXX: should this instead be called `.sendall()`?
    async def send(self, msg: MsgType) -> None:
        ...

    async def recv(self) -> MsgType:
        ...

    def __aiter__(self) -> MsgType:
        ...

    def connected(self) -> bool:
        ...

    # defining this sync otherwise it causes a mypy error because it
    # can't figure out it's a generator i guess?..?
    def drain(self) -> AsyncIterator[dict]:
        ...

    @property
    def laddr(self) -> tuple[str, int]:
        ...

    @property
    def raddr(self) -> tuple[str, int]:
        ...


# TODO: not sure why we have to inherit here, but it seems to be an
# issue with ``get_msg_transport()`` returning a ``Type[Protocol]``;
# probably should make a `mypy` issue?
class MsgpackTCPStream(MsgTransport):
    '''
    A ``trio.SocketStream`` delivering ``msgpack`` formatted data
    using the ``msgspec`` codec lib.

    '''
    def __init__(
        self,
        stream: trio.SocketStream,
        prefix_size: int = 4,

    ) -> None:

        self.stream = stream
        assert self.stream.socket

        # should both be IP sockets
        self._laddr, self._raddr = get_stream_addrs(stream)

        # create read loop instance
        self._agen = self._iter_packets()
        self._send_lock = trio.StrictFIFOLock()

        # public i guess?
        self.drained: list[dict] = []

        self.recv_stream = BufferedReceiveStream(transport_stream=stream)
        self.prefix_size = prefix_size

        # TODO: struct aware messaging coders
        self.encode = msgspec.msgpack.Encoder().encode
        self.decode = msgspec.msgpack.Decoder().decode  # dict[str, Any])

    async def _iter_packets(self) -> AsyncGenerator[dict, None]:
        '''Yield packets from the underlying stream.

        '''
        import msgspec  # noqa
        decodes_failed: int = 0

        while True:
            try:
                header = await self.recv_stream.receive_exactly(4)

            except (
                ValueError,
                ConnectionResetError,

                # not sure entirely why we need this but without it we
                # seem to be getting racy failures here on
                # arbiter/registry name subs..
                trio.BrokenResourceError,
            ):
                raise TransportClosed(
                    f'transport {self} was already closed prior ro read'
                )

            if header == b'':
                raise TransportClosed(
                    f'transport {self} was already closed prior ro read'
                )

            size, = struct.unpack("<I", header)

            log.transport(f'received header {size}')  # type: ignore

            msg_bytes = await self.recv_stream.receive_exactly(size)

            log.transport(f"received {msg_bytes}")  # type: ignore
            try:
                yield self.decode(msg_bytes)
            except (
                msgspec.DecodeError,
                UnicodeDecodeError,
            ):
                if decodes_failed < 4:
                    # ignore decoding errors for now and assume they have to
                    # do with a channel drop - hope that receiving from the
                    # channel will raise an expected error and bubble up.
                    try:
                        msg_str: str | bytes = msg_bytes.decode()
                    except UnicodeDecodeError:
                        msg_str = msg_bytes

                    log.error(
                        '`msgspec` failed to decode!?\n'
                        'dumping bytes:\n'
                        f'{msg_str!r}'
                    )
                    decodes_failed += 1
                else:
                    raise

    async def send(self, msg: Any) -> None:
        async with self._send_lock:

            bytes_data: bytes = self.encode(msg)

            # supposedly the fastest says,
            # https://stackoverflow.com/a/54027962
            size: bytes = struct.pack("<I", len(bytes_data))

            return await self.stream.send_all(size + bytes_data)

    @property
    def laddr(self) -> tuple[str, int]:
        return self._laddr

    @property
    def raddr(self) -> tuple[str, int]:
        return self._raddr

    async def recv(self) -> Any:
        return await self._agen.asend(None)

    async def drain(self) -> AsyncIterator[dict]:
        '''
        Drain the stream's remaining messages sent from
        the far end until the connection is closed by
        the peer.

        '''
        try:
            async for msg in self._iter_packets():
                self.drained.append(msg)
        except TransportClosed:
            for msg in self.drained:
                yield msg

    def __aiter__(self):
        return self._agen

    def connected(self) -> bool:
        return self.stream.socket.fileno() != -1


def get_msg_transport(

    key: tuple[str, str],

) -> Type[MsgTransport]:

    return {
        ('msgpack', 'tcp'): MsgpackTCPStream,
    }[key]


class Channel:
    '''
    An inter-process channel for communication between (remote) actors.

    Wraps a ``MsgStream``: transport + encoding IPC connection.

    Currently we only support ``trio.SocketStream`` for transport
    (aka TCP) and the ``msgpack`` interchange format via the ``msgspec``
    codec libary.

    '''
    def __init__(

        self,
        destaddr: Optional[tuple[str, int]],

        msg_transport_type_key: tuple[str, str] = ('msgpack', 'tcp'),

        # TODO: optional reconnection support?
        # auto_reconnect: bool = False,
        # on_reconnect: typing.Callable[..., typing.Awaitable] = None,

    ) -> None:

        # self._recon_seq = on_reconnect
        # self._autorecon = auto_reconnect

        self._destaddr = destaddr
        self._transport_key = msg_transport_type_key

        # Either created in ``.connect()`` or passed in by
        # user in ``.from_stream()``.
        self._stream: Optional[trio.SocketStream] = None
        self.msgstream: Optional[MsgTransport] = None

        # set after handshake - always uid of far end
        self.uid: Optional[tuple[str, str]] = None

        self._agen = self._aiter_recv()
        self._exc: Optional[Exception] = None  # set if far end actor errors
        self._closed: bool = False
        # flag set on ``Portal.cancel_actor()`` indicating
        # remote (peer) cancellation of the far end actor runtime.
        self._cancel_called: bool = False  # set on ``Portal.cancel_actor()``

    @classmethod
    def from_stream(
        cls,
        stream: trio.SocketStream,
        **kwargs,

    ) -> Channel:

        src, dst = get_stream_addrs(stream)
        chan = Channel(destaddr=dst, **kwargs)

        # set immediately here from provided instance
        chan._stream = stream
        chan.set_msg_transport(stream)
        return chan

    def set_msg_transport(
        self,
        stream: trio.SocketStream,
        type_key: Optional[tuple[str, str]] = None,

    ) -> MsgTransport:
        type_key = type_key or self._transport_key
        self.msgstream = get_msg_transport(type_key)(stream)
        return self.msgstream

    def __repr__(self) -> str:
        if self.msgstream:
            return repr(
                self.msgstream.stream.socket._sock).replace(  # type: ignore
                        "socket.socket", "Channel")
        return object.__repr__(self)

    @property
    def laddr(self) -> Optional[tuple[str, int]]:
        return self.msgstream.laddr if self.msgstream else None

    @property
    def raddr(self) -> Optional[tuple[str, int]]:
        return self.msgstream.raddr if self.msgstream else None

    async def connect(
        self,
        destaddr: tuple[Any, ...] = None,
        **kwargs

    ) -> MsgTransport:

        if self.connected():
            raise RuntimeError("channel is already connected?")

        destaddr = destaddr or self._destaddr
        assert isinstance(destaddr, tuple)

        stream = await trio.open_tcp_stream(
            *destaddr,
            **kwargs
        )
        msgstream = self.set_msg_transport(stream)

        log.transport(
            f'Opened channel[{type(msgstream)}]: {self.laddr} -> {self.raddr}'
        )
        return msgstream

    async def send(self, item: Any) -> None:

        log.transport(f"send `{item}`")  # type: ignore
        assert self.msgstream

        await self.msgstream.send(item)

    async def recv(self) -> Any:
        assert self.msgstream
        return await self.msgstream.recv()

        # try:
        #     return await self.msgstream.recv()
        # except trio.BrokenResourceError:
        #     if self._autorecon:
        #         await self._reconnect()
        #         return await self.recv()
        #     raise

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

    # async def _reconnect(self) -> None:
    #     """Handle connection failures by polling until a reconnect can be
    #     established.
    #     """
    #     down = False
    #     while True:
    #         try:
    #             with trio.move_on_after(3) as cancel_scope:
    #                 await self.connect()
    #             cancelled = cancel_scope.cancelled_caught
    #             if cancelled:
    #                 log.transport(
    #                     "Reconnect timed out after 3 seconds, retrying...")
    #                 continue
    #             else:
    #                 log.transport("Stream connection re-established!")

    #                 # TODO: run any reconnection sequence
    #                 # on_recon = self._recon_seq
    #                 # if on_recon:
    #                 #     await on_recon(self)

    #                 break
    #         except (OSError, ConnectionRefusedError):
    #             if not down:
    #                 down = True
    #                 log.transport(
    #                     f"Connection to {self.raddr} went down, waiting"
    #                     " for re-establishment")
    #             await trio.sleep(1)

    async def _aiter_recv(
        self
    ) -> AsyncGenerator[Any, None]:
        '''
        Async iterate items from underlying stream.

        '''
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

                # if not self._autorecon:
                raise

            await self.aclose()

            # if self._autorecon:  # attempt reconnect
            #     await self._reconnect()
            #     continue

    def connected(self) -> bool:
        return self.msgstream.connected() if self.msgstream else False


@asynccontextmanager
async def _connect_chan(
    host: str, port: int
) -> typing.AsyncGenerator[Channel, None]:
    '''
    Create and connect a channel with disconnect on context manager
    teardown.

    '''
    chan = Channel((host, port))
    await chan.connect()
    yield chan
    await chan.aclose()
