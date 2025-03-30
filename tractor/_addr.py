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
    TypeVar,
    Union,
    Type,
    TYPE_CHECKING,
)

from bidict import bidict
import trio
from trio import socket

from ._state import (
    get_rt_dir,
    current_actor,
)

if TYPE_CHECKING:
    from ._runtime import Actor


NamespaceType = TypeVar('NamespaceType')
AddressType = TypeVar('AddressType')
StreamType = TypeVar('StreamType')
ListenerType = TypeVar('ListenerType')


class Address(Protocol[
    NamespaceType,
    AddressType,
    StreamType,
    ListenerType
]):

    name_key: ClassVar[str]
    address_type: ClassVar[Type[AddressType]]

    @property
    def is_valid(self) -> bool:
        ...

    @property
    def namespace(self) -> NamespaceType|None:
        ...

    @classmethod
    def from_addr(cls, addr: AddressType) -> Address:
        ...

    def unwrap(self) -> AddressType:
        '''
        Deliver the underying minimum field set in
        a primitive python data type-structure.
        '''
        ...

    @classmethod
    def get_random(cls, namespace: NamespaceType | None = None) -> Address:
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

    # async def open_stream(self, **kwargs) -> StreamType:
    #     '''
    #     Open a connection *TO* this address and deliver back a
    #     `trio.SocketStream` wrapping the underlying transport.

    #     '''
    #     ...

    async def open_listener(self, **kwargs) -> ListenerType:
        ...

    async def close_listener(self):
        ...


class TCPAddress(Address[
    str,
    tuple[str, int],
    trio.SocketStream,
    trio.SocketListener
]):

    name_key: str = 'tcp'
    address_type: type = tuple[str, int]

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
    def namespace(self) -> str:
        return self._host

    @classmethod
    def from_addr(
        cls,
        addr: tuple[str, int]
    ) -> TCPAddress:
        return TCPAddress(addr[0], addr[1])

    def unwrap(self) -> tuple[str, int]:
        return (
            self._host,
            self._port,
        )

    @classmethod
    def get_random(
        cls,
        namespace: str = '127.0.0.1',
    ) -> TCPAddress:
        return TCPAddress(namespace, 0)

    @classmethod
    def get_root(cls) -> Address:
        return TCPAddress('127.0.0.1', 1616)

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

    # async def open_stream(self, **kwargs) -> trio.SocketStream:
    #     stream = await trio.open_tcp_stream(
    #         self._host,
    #         self._port,
    #         **kwargs
    #     )
    #     self._host, self._port = stream.socket.getsockname()[:2]
    #     return stream

    async def open_listener(self, **kwargs) -> trio.SocketListener:
        listeners = await trio.open_tcp_listeners(
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


class UDSAddress(Address[
    None,
    str,
    trio.SocketStream,
    trio.SocketListener
]):

    # TODO, maybe we should use 'unix' instead?
    # -[ ] need to check what other mult-transport frameworks do
    #     like zmq, nng, uri-spec et al!
    name_key: str = 'uds'
    address_type: type = tuple[str, int]

    def __init__(
        self,
        filepath: str|Path,
        maybe_pid: int,
        # ^XXX, in the sense you can also pass
        # a "non-real-world-process-id" such as is handy to represent
        # our host-local default "port-like" key for the very first
        # root actor to create a registry address.
    ):
        self._filepath: Path = Path(filepath).absolute()
        self._pid: int = maybe_pid

    @property
    def is_valid(self) -> bool:
        return True

    @property
    def namespace(self) -> None:
        return

    @classmethod
    def from_addr(
        cls,
        addr: tuple[Path, int]
    ) -> UDSAddress:
        return UDSAddress(
            filepath=addr[0],
            maybe_pid=addr[1],
        )

    def unwrap(self) -> tuple[Path, int]:
        return (
            str(self._filepath),
            # XXX NOTE, since this gets passed DIRECTLY to
            # `trio.open_unix_
            self._pid,
        )

    @classmethod
    def get_random(
        cls,
        namespace: None = None,  # unused
    ) -> UDSAddress:

        rt_dir: Path = get_rt_dir()
        pid: int = os.getpid()
        actor: Actor|None = current_actor(
            err_on_no_runtime=False,
        )
        if actor:
            sockname: str = '::'.join(actor.uid) + f'@{pid}'
        else:
            sockname: str = f'@{pid}'

        sockpath: Path = Path(f'{rt_dir}/{sockname}.sock')
        return UDSAddress(
            # filename=f'{tempfile.gettempdir()}/{uuid4()}.sock'
            filepath=sockpath,
            maybe_pid=pid,
        )

    @classmethod
    def get_root(cls) -> Address:
        def_uds_filepath: Path = (
            get_rt_dir()
            /
            'registry@1616.sock'
        )
        return UDSAddress(
            filepath=def_uds_filepath,
            maybe_pid=1616
        )

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}'
            f'['
            f'({self._filepath}, {self._pid})'
            f']'
        )

    def __eq__(self, other) -> bool:
        if not isinstance(other, UDSAddress):
            raise TypeError(
                f'Can not compare {type(other)} with {type(self)}'
            )

        return self._filepath == other._filepath

    # TODO? remove right, it's never used?
    #
    # async def open_stream(
    #     self,
    #     **kwargs,
    # ) -> trio.SocketStream:
    #     stream: trio.SocketStream = await trio.open_unix_socket(
    #         self._filepath,
    #         **kwargs
    #     )
    #     return stream

    async def open_listener(self, **kwargs) -> trio.SocketListener:
        self._sock = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM
        )
        await self._sock.bind(self._filepath)
        self._sock.listen(1)
        return trio.SocketListener(self._sock)

    async def close_listener(self):
        self._sock.close()
        os.unlink(self._filepath)


preferred_transport: str = 'uds'


_address_types: bidict[str, Type[Address]] = {
    'tcp': TCPAddress,
    'uds': UDSAddress
}


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
UnwrappedAddress = Union[
    tuple[
        str,  # (net/cgroup-)namespace/host-domain
        int,  # (p)id/port
    ]  # tcp/udp/uds

    # ?TODO? should we also include another 2 fields from
    # our `Aid` msg such that we include the runtime `Actor.uid`
    # of `.name` and `.uuid`?
    # - would ensure uniqueness across entire net?
    # - allows for easier runtime-level filtering of "actors by
    #   service name"
]


# TODO! really these are discovery sys default addrs ONLY useful for
# when none is provided to a root actor on first boot.
_default_lo_addrs: dict[
    str,
    UnwrappedAddress
] = {
    'tcp': TCPAddress(
        host='127.0.0.1',
        port=1616,
    ).unwrap(),
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

    if is_wrapped_addr(addr):
        return addr

    cls: Type|None = None
    match addr:
        case (
            str()|Path(),
            int(),
        ):
            cls = UDSAddress

        case tuple() | list():
            cls = TCPAddress

        case None:
            cls: Type[Address] = get_address_cls(preferred_transport)
            addr: AddressType = cls.get_root().unwrap()

        case _:
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
