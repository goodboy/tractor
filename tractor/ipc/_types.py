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

# the UDS backend is importable everywhere but only *usable* where
# `trio` reports `has_unix` (i.e. POSIX). On Windows / no-`AF_UNIX`
# hosts `HAS_UDS` is `False` and the runtime registers TCP only.
Address = TCPAddress|UDSAddress

# manually updated list of all supported msg transport types
_msg_transports: list[Type[MsgTransport]] = [
    MsgpackTCPStream,
]
if HAS_UDS:
    _msg_transports.append(MsgpackUDSStream)

# map a `MsgTransportKey` to its `MsgTransport` type
_key_to_transport: dict[MsgTransportKey, Type[MsgTransport]] = {
    ('msgpack', 'tcp'): MsgpackTCPStream,
}
if HAS_UDS:
    _key_to_transport[('msgpack', 'uds')] = MsgpackUDSStream

# map an `Address`-wrapper to its `MsgTransport` type
_addr_to_transport: dict[Type[Address], Type[MsgTransport]] = {
    TCPAddress: MsgpackTCPStream,
}
if HAS_UDS:
    _addr_to_transport[UDSAddress] = MsgpackUDSStream


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def transport_from_addr(
    addr: Address,
    codec_key: str = "msgpack",
) -> Type[MsgTransport]:
    """
    Given a destination address and a desired codec, find the
    corresponding `MsgTransport` type.
    """
    try:
        return _addr_to_transport[type(addr)]  # type: ignore[call-arg]
    except KeyError:
        raise NotImplementedError(
            f"No known transport for address {repr(addr)}"
        )


def transport_from_stream(
    stream: trio.abc.Stream,
    codec_key: str = "msgpack",
) -> Type[MsgTransport]:
    """
    Given an arbitrary `trio.abc.Stream` and a desired codec,
    find the corresponding `MsgTransport` type.
    """
    transport = None

    if isinstance(stream, trio.SocketStream):
        sock: socket.socket = stream.socket
        fam = sock.family

        if fam in (socket.AF_INET, getattr(socket, "AF_INET6", None)):
            transport = "tcp"

        # only consider `AF_UNIX` when the UDS backend is active;
        # `HAS_UDS` short-circuits before `socket.AF_UNIX` so this
        # stays safe on hosts where that constant is absent.
        if transport is None and HAS_UDS and fam == socket.AF_UNIX:  # type: ignore[attr-defined]
            transport = "uds"

        if transport is None:
            raise NotImplementedError(f"Unsupported socket family: {fam}")

    if not transport:
        raise NotImplementedError(
            f"Could not figure out transport type for stream type {type(stream)}"
        )

    key = (codec_key, transport)
    return _key_to_transport[key]

