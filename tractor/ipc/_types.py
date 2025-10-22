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
import logging
import platform
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

log = logging.getLogger(__name__)

# ------------------------------------------------------------
# Optional UDS backend (Windows / some Pythons may not have AF_UNIX)
# ------------------------------------------------------------
HAS_AF_UNIX = getattr(socket, "AF_UNIX", None) is not None
IS_WINDOWS = platform.system() == "Windows"

HAS_UDS = False
UDSAddress = None  # type: ignore
MsgpackUDSStream = None  # type: ignore

if HAS_AF_UNIX and not IS_WINDOWS:
    try:
        from tractor.ipc._uds import (  # type: ignore
            UDSAddress as _UDSAddress,
            MsgpackUDSStream as _MsgpackUDSStream,
        )
        UDSAddress = _UDSAddress  # type: ignore
        MsgpackUDSStream = _MsgpackUDSStream  # type: ignore
        HAS_UDS = True
    except Exception as e:
        log.warning("UDS backend unavailable (%s); continuing without it.", e)
else:
    if not HAS_AF_UNIX:
        log.warning("AF_UNIX not exposed by this Python; disabling UDS backend.")
    elif IS_WINDOWS:
        # Even if the Windows kernel supports AF_UNIX, CPython may not expose it,
        # and this project currently targets POSIX for the UDS backend.
        log.warning("Windows detected; disabling UDS backend.")

# ------------------------------------------------------------
# Public types and transport registries
# ------------------------------------------------------------

# Address is TCP-only unless UDS is available.
if HAS_UDS:
    Address = TCPAddress | UDSAddress  # type: ignore
else:
    Address = TCPAddress  # type: ignore

# Manually updated list of all supported msg transport types
_msg_transports: list[Type[MsgTransport]] = [
    MsgpackTCPStream,
]
if HAS_UDS:
    _msg_transports.append(MsgpackUDSStream)  # type: ignore

# Map MsgTransportKey -> transport type
_key_to_transport: dict[MsgTransportKey, Type[MsgTransport]] = {
    ("msgpack", "tcp"): MsgpackTCPStream,
}
if HAS_UDS:
    _key_to_transport[("msgpack", "uds")] = MsgpackUDSStream  # type: ignore

# Map Address wrapper -> transport type
_addr_to_transport: dict[Type[Address], Type[MsgTransport]] = {  # type: ignore
    TCPAddress: MsgpackTCPStream,
}
if HAS_UDS:
    _addr_to_transport[UDSAddress] = MsgpackUDSStream  # type: ignore


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

        # Only consider AF_UNIX when both Python exposes it and our backend is active
        if transport is None and HAS_UDS and HAS_AF_UNIX and fam == socket.AF_UNIX:  # type: ignore[attr-defined]
            transport = "uds"

        if transport is None:
            raise NotImplementedError(f"Unsupported socket family: {fam}")

    if not transport:
        raise NotImplementedError(
            f"Could not figure out transport type for stream type {type(stream)}"
        )

    key = (codec_key, transport)
    return _key_to_transport[key]

