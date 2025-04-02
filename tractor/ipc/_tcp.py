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

from tractor.msg import MsgCodec
from tractor.log import get_logger
from tractor._addr import TCPAddress
from tractor.ipc._transport import MsgpackTransport


log = get_logger(__name__)


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
