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
Unix Domain Socket implementation of tractor.ipc._transport.MsgTransport protocol 

'''
from __future__ import annotations

import trio

from tractor.msg import MsgCodec
from tractor.log import get_logger
from tractor._addr import UDSAddress
from tractor.ipc._transport import MsgpackTransport


log = get_logger(__name__)


class MsgpackUDSStream(MsgpackTransport):
    '''
    A ``trio.SocketStream`` delivering ``msgpack`` formatted data
    using the ``msgspec`` codec lib.

    '''
    address_type = UDSAddress
    layer_key: int = 7

    # def __init__(
    #     self,
    #     stream: trio.SocketStream,
    #     prefix_size: int = 4,
    #     codec: CodecType = None,

    # ) -> None:
    #     super().__init__(
    #         stream,
    #         prefix_size=prefix_size,
    #         codec=codec
    #     )

    @property
    def maddr(self) -> str:
        filepath = self.raddr.unwrap()
        return (
            f'/ipv4/localhost'
            f'/{self.address_type.name_key}/{filepath}'
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
        addr: UDSAddress,
        prefix_size: int = 4,
        codec: MsgCodec|None = None,
        **kwargs
    ) -> MsgpackUDSStream:
        stream = await trio.open_unix_socket(
            addr.unwrap(),
            **kwargs
        )
        return MsgpackUDSStream(
            stream,
            prefix_size=prefix_size,
            codec=codec
        )

    @classmethod
    def get_stream_addrs(
        cls,
        stream: trio.SocketStream
    ) -> tuple[UDSAddress, UDSAddress]:
        return (
            UDSAddress.from_addr(stream.socket.getsockname()),
            UDSAddress.from_addr(stream.socket.getsockname()),
        )
