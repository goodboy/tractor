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
from collections.abc import AsyncGenerator, AsyncIterator
from typing import (
    Any, Tuple, Optional,
    Type, Protocol, TypeVar,
)

from tricycle import BufferedReceiveStream
import msgpack
import trio
from async_generator import asynccontextmanager

from .log import get_logger
from ._exceptions import TransportClosed
log = get_logger(__name__)


_is_windows = platform.system() == 'Windows'
log = get_logger(__name__)


def get_stream_addrs(stream: trio.SocketStream) -> Tuple:
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
    def laddr(self) -> Tuple[str, int]:
        ...

    @property
    def raddr(self) -> Tuple[str, int]:
        ...


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
        self._laddr, self._raddr = get_stream_addrs(stream)

        # create read loop instance
        self._agen = self._iter_packets()
        self._send_lock = trio.StrictFIFOLock()

        # public i guess?
        self.drained: list[dict] = []

    async def _iter_packets(self) -> AsyncGenerator[dict, None]:
        """Yield packets from the underlying stream.
        """
        unpacker = msgpack.Unpacker(
            raw=False,
            use_list=False,
            strict_map_key=False
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
                    f'transport {self} was already closed prior to read'
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

    async def send(self, msg: Any) -> None:
        async with self._send_lock:
            return await self.stream.send_all(
                msgpack.dumps(msg, use_bin_type=True)
            )

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


class MsgspecTCPStream(MsgpackTCPStream):
    '''
    A ``trio.SocketStream`` delivering ``msgpack`` formatted data
    using ``msgspec``.

    '''
    def __init__(
        self,
        stream: trio.SocketStream,
        prefix_size: int = 4,

    ) -> None:
        import msgspec

        super().__init__(stream)
        self.recv_stream = BufferedReceiveStream(transport_stream=stream)
        self.prefix_size = prefix_size

        # TODO: struct aware messaging coders
        self.encode = msgspec.Encoder().encode
        self.decode = msgspec.Decoder().decode  # dict[str, Any])

    async def _iter_packets(self) -> AsyncGenerator[dict, None]:
        '''Yield packets from the underlying stream.

        '''
        import msgspec  # noqa
        last_decode_failed: bool = False

        while True:
            try:
                header = await self.recv_stream.receive_exactly(4)

            except (
                ValueError,

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
                msgspec.DecodingError,
                UnicodeDecodeError,
            ):
                if not last_decode_failed:
                    # ignore decoding errors for now and assume they have to
                    # do with a channel drop - hope that receiving from the
                    # channel will raise an expected error and bubble up.
                    log.error('`msgspec` failed to decode!?')
                    last_decode_failed = True
                else:
                    raise

    async def send(self, msg: Any) -> None:
        async with self._send_lock:

            bytes_data: bytes = self.encode(msg)

            # supposedly the fastest says,
            # https://stackoverflow.com/a/54027962
            size: bytes = struct.pack("<I", len(bytes_data))

            return await self.stream.send_all(size + bytes_data)


def get_msg_transport(

    key: Tuple[str, str],

) -> Type[MsgTransport]:

    return {
        ('msgpack', 'tcp'): MsgpackTCPStream,
        ('msgspec', 'tcp'): MsgspecTCPStream,
    }[key]


class Channel:
    '''
    An inter-process channel for communication between (remote) actors.

    Wraps a ``MsgStream``: transport + encoding IPC connection.
    Currently we only support ``trio.SocketStream`` for transport
    (aka TCP).

    '''
    def __init__(

        self,
        destaddr: Optional[Tuple[str, int]],

        msg_transport_type_key: Tuple[str, str] = ('msgpack', 'tcp'),

        # TODO: optional reconnection support?
        # auto_reconnect: bool = False,
        # on_reconnect: typing.Callable[..., typing.Awaitable] = None,

    ) -> None:

        # self._recon_seq = on_reconnect
        # self._autorecon = auto_reconnect

        # TODO: maybe expose this through the nursery api?
        try:
            # if installed load the msgspec transport since it's faster
            import msgspec  # noqa
            msg_transport_type_key = ('msgspec', 'tcp')
        except ImportError:
            pass

        self._destaddr = destaddr
        self._transport_key = msg_transport_type_key

        # Either created in ``.connect()`` or passed in by
        # user in ``.from_stream()``.
        self._stream: Optional[trio.SocketStream] = None
        self.msgstream: Optional[MsgTransport] = None

        # set after handshake - always uid of far end
        self.uid: Optional[Tuple[str, str]] = None

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
        type_key: Optional[Tuple[str, str]] = None,

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
    def laddr(self) -> Optional[Tuple[str, int]]:
        return self.msgstream.laddr if self.msgstream else None

    @property
    def raddr(self) -> Optional[Tuple[str, int]]:
        return self.msgstream.raddr if self.msgstream else None

    async def connect(
        self,
        destaddr: Tuple[Any, ...] = None,
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
