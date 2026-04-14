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
from contextlib import (
    contextmanager as cm,
)
from pathlib import Path
import os
import sys
from socket import (
    AF_UNIX,
    SOCK_STREAM,
    SOL_SOCKET,
    error as socket_error,
)
import struct
from typing import (
    Type,
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

from multiaddr import Multiaddr
from tractor.msg import MsgCodec
from tractor.log import get_logger
from tractor.discovery._multiaddr import mk_maddr
from tractor.ipc._transport import (
    MsgpackTransport,
)
from tractor.runtime._state import (
    get_rt_dir,
    current_actor,
    is_root_process,
)

if TYPE_CHECKING:
    from tractor.runtime._runtime import Actor


# Platform-specific credential passing constants
# See: https://stackoverflow.com/a/7982749
if sys.platform == 'linux':
    from socket import (
        SO_PASSCRED,
        SO_PEERCRED,
    )

else:
    # Other (Unix) platforms - though further testing is required and
    # others may need additional special handling?
    SO_PASSCRED = None
    SO_PEERCRED = None

    # NOTE, macOS uses `LOCAL_PEERCRED` instead of `SO_PEERCRED` and
    # doesn't need `SO_PASSCRED` (credential passing is always enabled).
    # See code in <sys/un.h>: `#define LOCAL_PEERCRED 0x001`
    #
    # XXX INSTEAD we use the (hopefully) more generic
    # `get_peer_pid()` below for other OSes.


log = get_logger()


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
            sockname: str = f'{actor.aid.name}@{pid}'
            # XXX, orig version which broke both macOS (file-name
            # length) and `multiaddrs` ('::' invalid separator).
            # sockname: str = '::'.join(actor.uid) + f'@{pid}'
            #
            # ?^TODO, for `multiaddr`'s parser we can't use the `::`
            # above^, SO maybe a `.` or something else here?
            # sockname: str = '.'.join(actor.uid) + f'@{pid}'
            # -[ ] CURRENTLY using `.` BREAKS TEST SUITE tho..
        else:
            if is_root_process():
                prefix: str = 'no_runtime_root'
            else:
                prefix: str = 'no_runtime_actor'

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
            f'({self.filedir}/, {self.filename}, {pid})'
        )
        return (
            f'{type(self).__name__}'
            f'['
            f'{body}'
            f']'
        )

@cm
def _reraise_as_connerr(
    src_excs: tuple[Type[Exception]],
    addr: UDSAddress,
):
    try:
        yield
    except src_excs as src_exc:
        raise ConnectionError(
            f'Bad UDS socket-filepath-as-address ??\n'
            f'{addr}\n'
            f' |_sockpath: {addr.sockpath}\n'
            f'\n'
            f'from src: {src_exc!r}\n'
        ) from src_exc


async def start_listener(
    addr: UDSAddress,
    **kwargs,
) -> SocketListener:
    '''
    Start listening for inbound connections via
    a `trio.SocketListener` (task) which `socket.bind()`s on `addr`.

    Note, if the `UDSAddress.bindspace: Path` directory dne it is
    implicitly created.

    '''
    sock = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM
    )
    log.info(
        f'Attempting to bind UDS socket\n'
        f'>[\n'
        f'|_{addr}\n'
    )

    # ?TODO? should we use the `actor.lifetime_stack`
    # to rm on shutdown?
    bindpath: Path = addr.sockpath
    if not (bs := addr.bindspace).is_dir():
        log.info(
            'Creating bindspace dir in file-sys\n'
            f'>{{\n'
            f'|_{bs!r}\n'
        )
        bs.mkdir()

    with _reraise_as_connerr(
        src_excs=(
            FileNotFoundError,
            OSError,
        ),
        addr=addr
    ):
        await sock.bind(str(bindpath))

    # NOTE, the backlog must be large enough to handle
    # concurrent connection attempts during actor teardown.
    # Previously this was `listen(1)` which caused
    # deregistration failures in the remote-daemon registrar
    # case: when multiple sub-actors simultaneously try to
    # connect to deregister, a backlog of 1 overflows and
    # connections get ECONNREFUSED. This matches the TCP
    # transport which uses `trio.open_tcp_listeners()` with
    # a default backlog of ~128.
    #
    # For details see the `close_listener()` below which
    # `os.unlink()`s the socket file on teardown — meaning
    # any NEW connection attempts after that point will fail
    # with `FileNotFoundError` regardless of backlog size.
    # The backlog only matters while the listener is alive
    # and accepting.
    sock.listen(128)
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

    NOTE, the `os.unlink()` here removes the socket file from
    the filesystem immediately, which means any subsequent
    connection attempts (e.g. sub-actors trying to deregister
    with a registrar whose listener is tearing down) will fail
    with `FileNotFoundError`. For the local-registrar case
    (parent IS the registrar), `_runtime.async_main()` works
    around this by reusing the existing `_parent_chan` instead
    of opening a new connection; see the `parent_is_reg` logic
    in the deregistration path.

    '''
    lstnr.socket.close()
    os.unlink(addr.sockpath)


async def open_unix_socket_w_passcred(
    filename: (
        str
        |bytes
        |os.PathLike[str]
        |os.PathLike[bytes]
    ),
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

    # Only set SO_PASSCRED on Linux (not needed/available on macOS)
    if SO_PASSCRED is not None:
        sock.setsockopt(SOL_SOCKET, SO_PASSCRED, 1)

    with close_on_error(sock):
        await sock.connect(os.fspath(filename))

    return trio.SocketStream(sock)


def get_peer_pid(sock) -> int|None:
    '''
    Gets the PID of the process connected to the other end of a Unix
    domain socket on macOS, or `None` if that fails.

    NOTE, should work on MacOS (and others?).

    '''
    # try to get the peer PID using a naive soln found from,
    # https://stackoverflow.com/a/67971484
    #
    # NOTE, a more correct soln is likely needed here according to
    # the complaints of `copilot` which led to digging into the
    # underlying `go`lang issue linked from the above SO answer,

    # XXX, darwin-xnu kernel srces defining these constants,
    # - SOL_LOCAL
    # |_https://github.com/apple/darwin-xnu/blob/main/bsd/sys/un.h#L85
    # - LOCAL_PEERPID
    # |_https://github.com/apple/darwin-xnu/blob/main/bsd/sys/un.h#L89
    #
    SOL_LOCAL: int = 0
    LOCAL_PEERPID: int = 0x002

    try:
        pid: int = sock.getsockopt(
            SOL_LOCAL,
            LOCAL_PEERPID,
        )
        return pid
    except socket_error as e:
        log.exception(
            f"Failed to get peer PID: {e}"
        )
        return None


def get_peer_info(
    sock: trio.socket.socket,
) -> tuple[
    int,  # pid
    int,  # uid
    int,  # guid
]:
    '''
    Deliver the connecting peer's "credentials"-info as defined in
    a platform-specific way.

    Linux-ONLY, uses SO_PEERCRED.

    For more deats see,
    - `man accept`,
    - `man unix`,

    this great online guide to all things sockets,
    - https://beej.us/guide/bgnet/html/split-wide/man-pages.html#setsockoptman

    AND this **wonderful SO answer**
    - https://stackoverflow.com/a/7982749

    '''
    if SO_PEERCRED is None:
        raise RuntimeError(
            f'Peer credential retrieval not supported on {sys.platform}!'
        )

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
    def maddr(self) -> Multiaddr|str:
        if not self.raddr:
            return '<unknown-peer>'

        return mk_maddr(self.raddr)

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

        with _reraise_as_connerr(
            src_excs=(
                FileNotFoundError,
            ),
            addr=addr
        ):
            stream = await open_unix_socket_w_passcred(
                str(sockpath),
                **kwargs
            )

        tpt_stream = MsgpackUDSStream(
            stream,
            prefix_size=prefix_size,
            codec=codec
        )
        # XXX assign from new addrs after peer-PID extract!
        (
            tpt_stream._laddr,
            tpt_stream._raddr,
        ) = cls.get_stream_addrs(stream)

        return tpt_stream

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

            case (str(), str()):  # XXX, likely macOS
                sock_path: Path = Path(peername)

            case _:
                raise TypeError(
                    f'Failed to match (peername, sockname) types?\n'
                    f'peername: {peername!r}\n'
                    f'sockname: {sockname!r}\n'
                )

        if sys.platform == 'linux':
            (
                peer_pid,
                _,
                _,
            ) = get_peer_info(sock)

        # NOTE known to at least works on,
        # - macos
        else:
            peer_pid: int|None = get_peer_pid(sock)
            if peer_pid is None:
                log.warning(
                    f'Unable to get peer PID?\n'
                    f'sock: {sock!r}\n'
                    f'peer_pid: {peer_pid!r}\n'
                )

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
