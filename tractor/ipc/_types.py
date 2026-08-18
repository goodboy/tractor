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
IPC subsys type-lookup helpers?

'''
from typing import Type
import socket
import trio

from tractor.ipc._transport import (
    MsgTransportKey,
    MsgTransport,
)
from tractor.ipc._tcp import (
    TCPAddress,
    MsgpackTCPStream,
)
from tractor.ipc._uds import (
    UDSAddress,
    MsgpackUDSStream,
    HAS_UDS,
)

# the UDS backend is importable everywhere but only *usable* when
# `HAS_UDS` is `True`; otherwise the runtime registers TCP only.
Address = TCPAddress|UDSAddress

# the available msg-transport backends on this host: TCP always,
# UDS only where usable (`HAS_UDS`). The lookup maps below derive
# from this single list via each backend's `codec_key` and
# `address_type`: register a backend here and every map picks it up.
_msg_transports: list[Type[MsgTransport]] = [
    MsgpackTCPStream,
]
if HAS_UDS:
    _msg_transports.append(MsgpackUDSStream)

# map a `MsgTransportKey` -> `MsgTransport` type
_key_to_transport: dict[MsgTransportKey, Type[MsgTransport]] = {
    (t.codec_key, t.address_type.proto_key): t
    for t in _msg_transports
}

# map an `Address`-wrapper -> `MsgTransport` type
_addr_to_transport: dict[Type[Address], Type[MsgTransport]] = {
    t.address_type: t
    for t in _msg_transports
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
        addr_type = type(addr)
        return _addr_to_transport[addr_type]

    except KeyError:
        raise NotImplementedError(
            f'No known transport for address '
            f'{addr!r}'
        )


def transport_from_stream(
    stream: trio.abc.Stream,
    codec_key: str = 'msgpack',
) -> Type[MsgTransport]:
    '''
    Given an arbitrary `trio.abc.Stream` and a desired codec,
    find the corresponding `MsgTransport` type.

    '''
    transport: str|None = None

    if isinstance(stream, trio.SocketStream):
        sock: socket.socket = stream.socket
        match sock.family:
            case socket.AF_INET | socket.AF_INET6:
                transport = 'tcp'

            # `HAS_UDS` short-circuits before `socket.AF_UNIX` on
            # hosts where that constant is absent.
            case fam if (
                HAS_UDS
                and
                fam == socket.AF_UNIX
            ):
                transport = 'uds'

            case fam:
                raise NotImplementedError(
                    f'Unsupported socket family: {fam}'
                )

    if not transport:
        raise NotImplementedError(
            f'Could not figure out transport type for stream type '
            f'{type(stream)}'
        )

    key = (codec_key, transport)

    return _key_to_transport[key]
