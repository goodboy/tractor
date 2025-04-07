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
TCP implementation of tractor.ipc._transport.MsgTransport protocol 

'''
from __future__ import annotations

import trio
from trio import (
    SocketListener,
    open_tcp_listeners,
)

from tractor.msg import MsgCodec
from tractor.log import get_logger
from tractor.ipc._transport import MsgpackTransport


log = get_logger(__name__)


class TCPAddress:
    proto_key: str = 'tcp'
    unwrapped_type: type = tuple[str, int]
    def_bindspace: str = '127.0.0.1'

    def __init__(
        self,
        host: str,
        port: int
    ):
        if (
            not isinstance(host, str)
            or
            not isinstance(port, int)
        ):
            raise TypeError(
                f'Expected host {host!r} to be str and port {port!r} to be int'
            )

        self._host: str = host
        self._port: int = port

    @property
    def is_valid(self) -> bool:
        return self._port != 0

    @property
    def bindspace(self) -> str:
        return self._host

    @property
    def domain(self) -> str:
        return self._host

    @classmethod
    def from_addr(
        cls,
        addr: tuple[str, int]
    ) -> TCPAddress:
        match addr:
            case (str(), int()):
                return TCPAddress(addr[0], addr[1])
            case _:
                raise ValueError(
                    f'Invalid unwrapped address for {cls}\n'
                    f'{addr}\n'
                )

    def unwrap(self) -> tuple[str, int]:
        return (
            self._host,
            self._port,
        )

    @classmethod
    def get_random(
        cls,
        bindspace: str = def_bindspace,
    ) -> TCPAddress:
        return TCPAddress(bindspace, 0)

    @classmethod
    def get_root(cls) -> TCPAddress:
        return TCPAddress(
            '127.0.0.1',
            1616,
        )

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}[{self.unwrap()}]'
        )

    def __eq__(self, other) -> bool:
        if not isinstance(other, TCPAddress):
            raise TypeError(
                f'Can not compare {type(other)} with {type(self)}'
            )

        return (
            self._host == other._host
            and
            self._port == other._port
        )

    async def open_listener(
        self,
        **kwargs,
    ) -> SocketListener:
        listeners: list[SocketListener] = await open_tcp_listeners(
            host=self._host,
            port=self._port,
            **kwargs
        )
        assert len(listeners) == 1
        listener = listeners[0]
        self._host, self._port = listener.socket.getsockname()[:2]
        return listener

    async def close_listener(self):
        ...


# TODO: typing oddity.. not sure why we have to inherit here, but it
# seems to be an issue with `get_msg_transport()` returning
# a `Type[Protocol]`; probably should make a `mypy` issue?
class MsgpackTCPStream(MsgpackTransport):
    '''
    A ``trio.SocketStream`` delivering ``msgpack`` formatted data
    using the ``msgspec`` codec lib.

    '''
    address_type = TCPAddress
    layer_key: int = 4

    @property
    def maddr(self) -> str:
        host, port = self.raddr.unwrap()
        return (
            # TODO, use `ipaddress` from stdlib to handle
            # first detecting which of `ipv4/6` before
            # choosing the routing prefix part.
            f'/ipv4/{host}'

            f'/{self.address_type.proto_key}/{port}'
            # f'/{self.chan.uid[0]}'
            # f'/{self.cid}'

            # f'/cid={cid_head}..{cid_tail}'
            # TODO: ? not use this ^ right ?
        )

    def connected(self) -> bool:
        return self.stream.socket.fileno() != -1

    @classmethod
    async def connect_to(
        cls,
        destaddr: TCPAddress,
        prefix_size: int = 4,
        codec: MsgCodec|None = None,
        **kwargs
    ) -> MsgpackTCPStream:
        stream = await trio.open_tcp_stream(
            *destaddr.unwrap(),
            **kwargs
        )
        return MsgpackTCPStream(
            stream,
            prefix_size=prefix_size,
            codec=codec
        )

    @classmethod
    def get_stream_addrs(
        cls,
        stream: trio.SocketStream
    ) -> tuple[
        TCPAddress,
        TCPAddress,
    ]:
        # TODO, what types are these?
        lsockname = stream.socket.getsockname()
        l_sockaddr: tuple[str, int] = tuple(lsockname[:2])
        rsockname = stream.socket.getpeername()
        r_sockaddr: tuple[str, int] = tuple(rsockname[:2])
        return (
            TCPAddress.from_addr(l_sockaddr),
            TCPAddress.from_addr(r_sockaddr),
        )
