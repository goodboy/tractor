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
from __future__ import annotations
from pathlib import Path
import os
# import tempfile
from uuid import uuid4
from typing import (
    Protocol,
    ClassVar,
    # TypeVar,
    # Union,
    Type,
    TYPE_CHECKING,
)

from bidict import bidict
# import trio
from trio import (
    socket,
    SocketListener,
    open_tcp_listeners,
)

from .log import get_logger
from ._state import (
    get_rt_dir,
    current_actor,
    is_root_process,
    _def_tpt_proto,
)

if TYPE_CHECKING:
    from ._runtime import Actor

log = get_logger(__name__)


# TODO, maybe breakout the netns key to a struct?
# class NetNs(Struct)[str, int]:
#     ...

# TODO, can't we just use a type alias
# for this? namely just some `tuple[str, int, str, str]`?
#
# -[ ] would also just be simpler to keep this as SockAddr[tuple]
#     or something, implying it's just a simple pair of values which can
#     presumably be mapped to all transports?
# -[ ] `pydoc socket.socket.getsockname()` delivers a 4-tuple for
#     ipv6 `(hostaddr, port, flowinfo, scope_id)`.. so how should we
#     handle that?
# -[ ] as a further alternative to this wrap()/unwrap() approach we
#     could just implement `enc/dec_hook()`s for the `Address`-types
#     and just deal with our internal objs directly and always and
#     leave it to the codec layer to figure out marshalling?
#    |_ would mean only one spot to do the `.unwrap()` (which we may
#       end up needing to call from the hook()s anyway?)
# -[x] rename to `UnwrappedAddress[Descriptor]` ??
#    seems like the right name as per,
#    https://www.geeksforgeeks.org/introduction-to-address-descriptor/
#
UnwrappedAddress = (
    # tcp/udp/uds
    tuple[
        str,  # host/domain(tcp), filesys-dir(uds)
        int|str,  # port/path(uds)
    ]
    # ?TODO? should we also include another 2 fields from
    # our `Aid` msg such that we include the runtime `Actor.uid`
    # of `.name` and `.uuid`?
    # - would ensure uniqueness across entire net?
    # - allows for easier runtime-level filtering of "actors by
    #   service name"
)


# TODO, maybe rename to `SocketAddress`?
class Address(Protocol):
    proto_key: ClassVar[str]
    unwrapped_type: ClassVar[UnwrappedAddress]

    # TODO, i feel like an `.is_bound()` is a better thing to
    # support?
    # Lke, what use does this have besides a noop and if it's not
    # valid why aren't we erroring on creation/use?
    @property
    def is_valid(self) -> bool:
        ...

    # TODO, maybe `.netns` is a better name?
    @property
    def namespace(self) -> tuple[str, int]|None:
        '''
        The if-available, OS-specific "network namespace" key.

        '''
        ...

    @property
    def bindspace(self) -> str:
        '''
        Deliver the socket address' "bindable space" from
        a `socket.socket.bind()` and thus from the perspective of
        specific transport protocol domain.

        I.e. for most (layer-4) network-socket protocols this is
        normally the ipv4/6 address, for UDS this is normally
        a filesystem (sub-directory).

        For (distributed) network protocols this is normally the routing
        layer's domain/(ip-)address, though it might also include a "network namespace"
        key different then the default.

        For local-host-only transports this is either an explicit
        namespace (with types defined by the OS: netns, Cgroup, IPC,
        pid, etc. on linux) or failing that the sub-directory in the
        filesys in which socket/shm files are located *under*.

        '''
        ...

    @classmethod
    def from_addr(cls, addr: UnwrappedAddress) -> Address:
        ...

    def unwrap(self) -> UnwrappedAddress:
        '''
        Deliver the underying minimum field set in
        a primitive python data type-structure.
        '''
        ...

    @classmethod
    def get_random(
        cls,
        current_actor: Actor,
        bindspace: str|None = None,
    ) -> Address:
        ...

    # TODO, this should be something like a `.get_def_registar_addr()`
    # or similar since,
    # - it should be a **host singleton** (not root/tree singleton)
    # - we **only need this value** when one isn't provided to the
    #   runtime at boot and we want to implicitly provide a host-wide
    #   registrar.
    # - each rooted-actor-tree should likely have its own
    #   micro-registry (likely the root being it), also see
    @classmethod
    def get_root(cls) -> Address:
        ...

    def __repr__(self) -> str:
        ...

    def __eq__(self, other) -> bool:
        ...

    async def open_listener(
        self,
        **kwargs,
    ) -> SocketListener:
        ...

    async def close_listener(self):
        ...


class TCPAddress(Address):
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
    def get_root(cls) -> Address:
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


def unwrap_sockpath(
    sockpath: Path,
) -> tuple[Path, Path]:
    return (
        sockpath.parent,
        sockpath.name,
    )


class UDSAddress(Address):
    # TODO, maybe we should use better field and value
    # -[x] really this is a `.protocol_key` not a "name" of anything.
    # -[ ] consider a 'unix' proto-key instead?
    # -[ ] need to check what other mult-transport frameworks do
    #     like zmq, nng, uri-spec et al!
    proto_key: str = 'uds'
    unwrapped_type: type = tuple[str, int]
    def_bindspace: Path = get_rt_dir()

    def __init__(
        self,
        filedir: Path|str|None,
        # TODO, i think i want `.filename` here?
        filename: str|Path,

        # XXX, in the sense you can also pass
        # a "non-real-world-process-id" such as is handy to represent
        # our host-local default "port-like" key for the very first
        # root actor to create a registry address.
        maybe_pid: int|None = None,
    ):
        fdir = self._filedir = Path(filedir or self.def_bindspace).absolute()
        fpath = self._filepath = Path(filename)
        fp: Path = fdir / fpath
        assert fp.is_absolute()

        # to track which "side" is the peer process by reading socket
        # credentials-info.
        self._pid: int = maybe_pid

    @property
    def sockpath(self) -> Path:
        return self._filedir / self._filepath

    @property
    def is_valid(self) -> bool:
        '''
        We block socket files not allocated under the runtime subdir.

        '''
        return self.bindspace in self.sockpath.parents

    @property
    def bindspace(self) -> Path:
        '''
        We replicate the "ip-set-of-hosts" part of a UDS socket as
        just the sub-directory in which we allocate socket files.

        '''
        return self._filedir or self.def_bindspace

    @classmethod
    def from_addr(
        cls,
        addr: (
            tuple[Path|str|None, int]
            |Path|str
        ),
    ) -> UDSAddress:
        match addr:
            case tuple()|list():
                sockpath: Path = Path(addr[0])
                filedir, filename = unwrap_sockpath(sockpath)
                pid: int = addr[1]
                return UDSAddress(
                    filedir=filedir,
                    filename=filename,
                    maybe_pid=pid,
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
            str(self.sockpath),
            self._pid,
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
    def get_root(cls) -> Address:
        def_uds_filepath: Path = 'registry@1616.sock'
        return UDSAddress(
            filedir=None,
            filename=def_uds_filepath,
            maybe_pid=1616,
        )

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}'
            f'['
            f'({self.sockpath}, {self._pid})'
            f']'
        )

    def __eq__(self, other) -> bool:
        if not isinstance(other, UDSAddress):
            raise TypeError(
                f'Can not compare {type(other)} with {type(self)}'
            )

        return self._filepath == other._filepath

    # async def open_listener(self, **kwargs) -> SocketListener:
    async def open_listener(
        self,
        **kwargs,
    ) -> SocketListener:
        sock = self._sock = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM
        )
        log.info(
            f'Attempting to bind UDS socket\n'
            f'>[\n'
            f'|_{self}\n'
        )

        bindpath: Path = self.sockpath
        await sock.bind(str(bindpath))
        sock.listen(1)
        log.info(
            f'Listening on UDS socket\n'
            f'[>\n'
            f' |_{self}\n'
        )
        return SocketListener(self._sock)

    def close_listener(self):
        self._sock.close()
        os.unlink(self.sockpath)


_address_types: bidict[str, Type[Address]] = {
    'tcp': TCPAddress,
    'uds': UDSAddress
}


# TODO! really these are discovery sys default addrs ONLY useful for
# when none is provided to a root actor on first boot.
_default_lo_addrs: dict[
    str,
    UnwrappedAddress
] = {
    'tcp': TCPAddress.get_root().unwrap(),
    'uds': UDSAddress.get_root().unwrap(),
}


def get_address_cls(name: str) -> Type[Address]:
    return _address_types[name]


def is_wrapped_addr(addr: any) -> bool:
    return type(addr) in _address_types.values()


def mk_uuid() -> str:
    '''
    Encapsulate creation of a uuid4 as `str` as used
    for creating `Actor.uid: tuple[str, str]` and/or
    `.msg.types.Aid`.

    '''
    return str(uuid4())


def wrap_address(
    addr: UnwrappedAddress
) -> Address:
    '''
    Wrap an `UnwrappedAddress` as an `Address`-type based
    on matching builtin python data-structures which we adhoc
    use for each.

    XXX NOTE, careful care must be placed to ensure
    `UnwrappedAddress` cases are **definitely unique** otherwise the
    wrong transport backend may be loaded and will break many
    low-level things in our runtime in a not-fun-to-debug way!

    XD

    '''
    if is_wrapped_addr(addr):
        return addr

    cls: Type|None = None
    # if 'sock' in addr[0]:
    #     import pdbp; pdbp.set_trace()
    match addr:
        # TODO! BUT THIS WILL MATCH FOR TCP !...
        # -[ ] so prolly go back to what guille had orig XD
        #   a plain ol' `str`?
        # case ((
        #     str()|Path(),
        #     int(),
        # )):
        #     cls = UDSAddress

        # classic network socket-address as tuple/list
        case (
            (str(), int())
            |
            [str(), int()]
        ):
            cls = TCPAddress

        # likely an unset UDS or TCP reg address as defaulted in
        # `_state._runtime_vars['_root_mailbox']`
        #
        # TODO? figure out when/if we even need this?
        case (
            None
            |
            [None, None]
        ):
            cls: Type[Address] = get_address_cls(_def_tpt_proto)
            addr: UnwrappedAddress = cls.get_root().unwrap()

        case _:
            # import pdbp; pdbp.set_trace()
            raise TypeError(
                f'Can not wrap address {type(addr)}\n'
                f'{addr!r}\n'
            )

    return cls.from_addr(addr)


def default_lo_addrs(
    transports: list[str],
) -> list[Type[Address]]:
    '''
    Return the default, host-singleton, registry address
    for an input transport key set.

    '''
    return [
        _default_lo_addrs[transport]
        for transport in transports
    ]
