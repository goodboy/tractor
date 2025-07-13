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
    AF_UNIX,
    SOCK_STREAM,
    SO_PASSCRED,
    SO_PEERCRED,
    SOL_SOCKET,
)
import struct
from typing import (
    TYPE_CHECKING,
    ClassVar,
)

import msgspec
import trio
from trio import (
    socket,
    SocketListener,
)
from trio._highlevel_open_unix_stream import (
    close_on_error,
    has_unix,
)

from tractor.msg import MsgCodec
from tractor.log import get_logger
from tractor.ipc._transport import (
    MsgpackTransport,
)
from .._state import (
    get_rt_dir,
    current_actor,
    is_root_process,
)

if TYPE_CHECKING:
    from ._runtime import Actor


log = get_logger(__name__)


def unwrap_sockpath(
    sockpath: Path,
) -> tuple[Path, Path]:
    return (
        sockpath.parent,
        sockpath.name,
    )


class UDSAddress(
    msgspec.Struct,
    frozen=True,
):
    filedir: str|Path|None
    filename: str|Path
    maybe_pid: int|None = None

    # TODO, maybe we should use better field and value
    # -[x] really this is a `.protocol_key` not a "name" of anything.
    # -[ ] consider a 'unix' proto-key instead?
    # -[ ] need to check what other mult-transport frameworks do
    #     like zmq, nng, uri-spec et al!
    proto_key: ClassVar[str] = 'uds'
    unwrapped_type: ClassVar[type] = tuple[str, int]
    def_bindspace: ClassVar[Path] = get_rt_dir()

    @property
    def bindspace(self) -> Path:
        '''
        We replicate the "ip-set-of-hosts" part of a UDS socket as
        just the sub-directory in which we allocate socket files.

        '''
        return (
            self.filedir
            or
            self.def_bindspace
            # or
            # get_rt_dir()
        )

    @property
    def sockpath(self) -> Path:
        return self.bindspace / self.filename

    @property
    def is_valid(self) -> bool:
        '''
        We block socket files not allocated under the runtime subdir.

        '''
        return self.bindspace in self.sockpath.parents

    @classmethod
    def from_addr(
        cls,
        addr: (
            tuple[Path|str, Path|str]|Path|str
        ),
    ) -> UDSAddress:
        match addr:
            case tuple()|list():
                filedir = Path(addr[0])
                filename = Path(addr[1])
                return UDSAddress(
                    filedir=filedir,
                    filename=filename,
                    # maybe_pid=pid,
                )
            # NOTE, in case we ever decide to just `.unwrap()`
            # to a `Path|str`?
            case str()|Path():
                sockpath: Path = Path(addr)
                return UDSAddress(*unwrap_sockpath(sockpath))
            case _:
                # import pdbp; pdbp.set_trace()
                raise TypeError(
                    f'Bad unwrapped-address for {cls} !\n'
                    f'{addr!r}\n'
                )

    def unwrap(self) -> tuple[str, int]:
        # XXX NOTE, since this gets passed DIRECTLY to
        # `.ipc._uds.open_unix_socket_w_passcred()`
        return (
            str(self.filedir),
            str(self.filename),
        )

    @classmethod
    def get_random(
        cls,
        bindspace: Path|None = None,  # default netns
    ) -> UDSAddress:

        filedir: Path = bindspace or cls.def_bindspace
        pid: int = os.getpid()
        actor: Actor|None = current_actor(
            err_on_no_runtime=False,
        )
        if actor:
            sockname: str = '::'.join(actor.uid) + f'@{pid}'
        else:
            prefix: str = '<unknown-actor>'
            if is_root_process():
                prefix: str = 'root'
            sockname: str = f'{prefix}@{pid}'

        sockpath: Path = Path(f'{sockname}.sock')
        return UDSAddress(
            filedir=filedir,
            filename=sockpath,
            maybe_pid=pid,
        )

    @classmethod
    def get_root(cls) -> UDSAddress:
        def_uds_filename: Path = 'registry@1616.sock'
        return UDSAddress(
            filedir=cls.def_bindspace,
            filename=def_uds_filename,
            # maybe_pid=1616,
        )

    # ?TODO, maybe we should just our .msg.pretty_struct.Struct` for
    # this instead?
    # -[ ] is it too "multi-line"y tho?
    #      the compact tuple/.unwrapped() form is simple enough?
    #
    def __repr__(self) -> str:
        if not (pid := self.maybe_pid):
            pid: str = '<unknown-peer-pid>'

        body: str = (
            f'({self.filedir}, {self.filename}, {pid})'
        )
        return (
            f'{type(self).__name__}'
            f'['
            f'{body}'
            f']'
        )


async def start_listener(
    addr: UDSAddress,
    **kwargs,
) -> SocketListener:
    # sock = addr._sock = socket.socket(
    sock = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM
    )
    log.info(
        f'Attempting to bind UDS socket\n'
        f'>[\n'
        f'|_{addr}\n'
    )

    bindpath: Path = addr.sockpath
    try:
        await sock.bind(str(bindpath))
    except (
        FileNotFoundError,
    ) as fdne:
        raise ConnectionError(
            f'Bad UDS socket-filepath-as-address ??\n'
            f'{addr}\n'
            f' |_sockpath: {addr.sockpath}\n'
        ) from fdne

    sock.listen(1)
    log.info(
        f'Listening on UDS socket\n'
        f'[>\n'
        f' |_{addr}\n'
    )
    return SocketListener(sock)


def close_listener(
    addr: UDSAddress,
    lstnr: SocketListener,
) -> None:
    '''
    Close and remove the listening unix socket's path.

    '''
    lstnr.socket.close()
    os.unlink(addr.sockpath)


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
