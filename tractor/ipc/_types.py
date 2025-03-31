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
from typing import Type

import trio
import socket

from tractor._addr import Address
from tractor.ipc._transport import (
    MsgTransportKey,
    MsgTransport
)
from tractor.ipc._tcp import MsgpackTCPStream
from tractor.ipc._uds import MsgpackUDSStream


# manually updated list of all supported msg transport types
_msg_transports = [
    MsgpackTCPStream,
    MsgpackUDSStream
]


# convert a MsgTransportKey to the corresponding transport type
_key_to_transport: dict[MsgTransportKey, Type[MsgTransport]] = {
    cls.key(): cls
    for cls in _msg_transports
}

# convert an Address wrapper to its corresponding transport type
_addr_to_transport: dict[Type[Address], Type[MsgTransport]] = {
    cls.address_type: cls
    for cls in _msg_transports
}


def transport_from_addr(
    addr: Address,
    codec_key: str = 'msgpack',
) -> Type[MsgTransport]:
    '''
    Given a destination address and a desired codec, find the
    corresponding `MsgTransport` type.

    '''
    try:
        return _addr_to_transport[type(addr)]

    except KeyError:
        raise NotImplementedError(
            f'No known transport for address {repr(addr)}'
        )


def transport_from_stream(
    stream: trio.abc.Stream,
    codec_key: str = 'msgpack'
) -> Type[MsgTransport]:
    '''
    Given an arbitrary `trio.abc.Stream` and a desired codec,
    find the corresponding `MsgTransport` type.

    '''
    transport = None
    if isinstance(stream, trio.SocketStream):
        sock: socket.socket = stream.socket
        match sock.family:
            case socket.AF_INET | socket.AF_INET6:
                transport = 'tcp'

            case socket.AF_UNIX:
                transport = 'uds'

            case _:
                raise NotImplementedError(
                    f'Unsupported socket family: {sock.family}'
                )

    if not transport:
        raise NotImplementedError(
            f'Could not figure out transport type for stream type {type(stream)}'
        )

    key = (codec_key, transport)

    return _key_to_transport[key]
