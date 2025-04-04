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
from pathlib import Path
import os
from socket import (
    # socket,
    AF_UNIX,
    SOCK_STREAM,
    SO_PASSCRED,
    SO_PEERCRED,
    SOL_SOCKET,
)
import struct

import trio
from trio._highlevel_open_unix_stream import (
    close_on_error,
    has_unix,
)

from tractor.msg import MsgCodec
from tractor.log import get_logger
from tractor._addr import (
    UDSAddress,
    unwrap_sockpath,
)
from tractor.ipc._transport import MsgpackTransport


log = get_logger(__name__)


async def open_unix_socket_w_passcred(
    filename: str|bytes|os.PathLike[str]|os.PathLike[bytes],
) -> trio.SocketStream:
    '''
    Literally the exact same as `trio.open_unix_socket()` except we set the additiona
    `socket.SO_PASSCRED` option to ensure the server side (the process calling `accept()`)
    can extract the connecting peer's credentials, namely OS specific process
    related IDs.

    See this SO for "why" the extra opts,
    - https://stackoverflow.com/a/7982749

    '''
    if not has_unix:
        raise RuntimeError("Unix sockets are not supported on this platform")

    # much more simplified logic vs tcp sockets - one socket type and only one
    # possible location to connect to
    sock = trio.socket.socket(AF_UNIX, SOCK_STREAM)
    sock.setsockopt(SOL_SOCKET, SO_PASSCRED, 1)
    with close_on_error(sock):
        await sock.connect(os.fspath(filename))

    return trio.SocketStream(sock)


def get_peer_info(sock: trio.socket.socket) -> tuple[
    int,  # pid
    int,  # uid
    int,  # guid
]:
    '''
    Deliver the connecting peer's "credentials"-info as defined in
    a very Linux specific way..

    For more deats see,
    - `man accept`,
    - `man unix`,

    this great online guide to all things sockets,
    - https://beej.us/guide/bgnet/html/split-wide/man-pages.html#setsockoptman

    AND this **wonderful SO answer**
    - https://stackoverflow.com/a/7982749

    '''
    creds: bytes = sock.getsockopt(
        SOL_SOCKET,
        SO_PEERCRED,
        struct.calcsize('3i')
    )
    # i.e a tuple of the fields,
    # pid: int, "process"
    # uid: int, "user"
    # gid: int, "group"
    return struct.unpack('3i', creds)


class MsgpackUDSStream(MsgpackTransport):
    '''
    A `trio.SocketStream` around a Unix-Domain-Socket transport
    delivering `msgpack` encoded msgs using the `msgspec` codec lib.

    '''
    address_type = UDSAddress
    layer_key: int = 4

    @property
    def maddr(self) -> str:
        if not self.raddr:
            return '<unknown-peer>'

        filepath: Path = Path(self.raddr.unwrap()[0])
        return (
            f'/{self.address_type.proto_key}/{filepath}'
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


        sockpath: Path = addr.sockpath
        #
        # ^XXX NOTE, we don't provide any out-of-band `.pid` info
        # (like, over the socket as extra msgs) since the (augmented)
        # `.setsockopt()` call tells the OS provide it; the client
        # pid can then be read on server/listen() side via
        # `get_peer_info()` above.
        try:
            stream = await open_unix_socket_w_passcred(
                str(sockpath),
                **kwargs
            )
        except (
            FileNotFoundError,
        ) as fdne:
            raise ConnectionError(
                f'Bad UDS socket-filepath-as-address ??\n'
                f'{addr}\n'
                f' |_sockpath: {sockpath}\n'
            ) from fdne

        stream = MsgpackUDSStream(
            stream,
            prefix_size=prefix_size,
            codec=codec
        )
        stream._raddr = addr
        return stream

    @classmethod
    def get_stream_addrs(
        cls,
        stream: trio.SocketStream
    ) -> tuple[
        Path,
        int,
    ]:
        sock: trio.socket.socket = stream.socket

        # NOTE XXX, it's unclear why one or the other ends up being
        # `bytes` versus the socket-file-path, i presume it's
        # something to do with who is the server (called `.listen()`)?
        # maybe could be better implemented using another info-query
        # on the socket like,
        # https://beej.us/guide/bgnet/html/split-wide/system-calls-or-bust.html#gethostnamewho-am-i
        sockname: str|bytes = sock.getsockname()
        # https://beej.us/guide/bgnet/html/split-wide/system-calls-or-bust.html#getpeernamewho-are-you
        peername: str|bytes = sock.getpeername()
        match (peername, sockname):
            case (str(), bytes()):
                sock_path: Path = Path(peername)
            case (bytes(), str()):
                sock_path: Path = Path(sockname)
        (
            peer_pid,
            _,
            _,
        ) = get_peer_info(sock)

        filedir, filename = unwrap_sockpath(sock_path)
        laddr = UDSAddress(
            filedir=filedir,
            filename=filename,
            maybe_pid=os.getpid(),
        )
        raddr = UDSAddress(
            filedir=filedir,
            filename=filename,
            maybe_pid=peer_pid
        )
        return (laddr, raddr)
