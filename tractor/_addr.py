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
import tempfile
from uuid import uuid4
from typing import (
    Protocol,
    ClassVar,
    TypeVar,
    Union,
    Type
)

import trio
from trio import socket


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
        ...

    @classmethod
    def get_random(cls, namespace: NamespaceType | None = None) -> Address:
        ...

    @classmethod
    def get_root(cls) -> Address:
        ...

    def __repr__(self) -> str:
        ...

    def __eq__(self, other) -> bool:
        ...

    async def open_stream(self, **kwargs) -> StreamType:
        ...

    async def open_listener(self, **kwargs) -> ListenerType:
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
            raise TypeError(f'Expected host {host} to be str and port {port} to be int')
        self._host = host
        self._port = port

    @property
    def is_valid(self) -> bool:
        return self._port != 0

    @property
    def namespace(self) -> str:
        return self._host

    @classmethod
    def from_addr(cls, addr: tuple[str, int]) -> TCPAddress:
        return TCPAddress(addr[0], addr[1])

    def unwrap(self) -> tuple[str, int]:
        return self._host, self._port

    @classmethod
    def get_random(cls, namespace: str = '127.0.0.1') -> TCPAddress:
        return TCPAddress(namespace, 0)

    @classmethod
    def get_root(cls) -> Address:
        return TCPAddress('127.0.0.1', 1616)

    def __repr__(self) -> str:
        return f'{type(self)} @ {self.unwrap()}'

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

    async def open_stream(self, **kwargs) -> trio.SocketStream:
        stream = await trio.open_tcp_stream(
            self._host,
            self._port,
            **kwargs
        )
        self._host, self._port = stream.socket.getsockname()[:2]
        return stream

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


class UDSAddress(Address[
    None,
    str,
    trio.SocketStream,
    trio.SocketListener
]):

    name_key: str = 'uds'
    address_type: type = str

    def __init__(
        self,
        filepath: str
    ):
        self._filepath = filepath

    @property
    def is_valid(self) -> bool:
        return True

    @property
    def namespace(self) -> None:
        return

    @classmethod
    def from_addr(cls, filepath: str) -> UDSAddress:
        return UDSAddress(filepath)

    def unwrap(self) -> str:
        return self._filepath

    @classmethod
    def get_random(cls, _ns: None = None) -> UDSAddress:
        return UDSAddress(f'{tempfile.gettempdir()}/{uuid4().sock}')

    @classmethod
    def get_root(cls) -> Address:
        return UDSAddress('tractor.sock')

    def __repr__(self) -> str:
        return f'{type(self)} @ {self._filepath}'

    def __eq__(self, other) -> bool:
        if not isinstance(other, UDSAddress):
            raise TypeError(
                f'Can not compare {type(other)} with {type(self)}'
            )

        return self._filepath == other._filepath

    async def open_stream(self, **kwargs) -> trio.SocketStream:
        stream = await trio.open_tcp_stream(
            self._filepath,
            **kwargs
        )
        self._binded = True
        return stream

    async def open_listener(self, **kwargs) -> trio.SocketListener:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(self._filepath)
        sock.listen()
        self._binded = True
        return trio.SocketListener(sock)


preferred_transport = 'tcp'


_address_types = (
    TCPAddress,
    UDSAddress
)


_default_addrs: dict[str, Type[Address]] = {
    cls.name_key: cls
    for cls in _address_types
}


AddressTypes = Union[
    tuple([
        cls.address_type
        for cls in _address_types
    ])
]


_default_lo_addrs: dict[
    str,
    AddressTypes
] = {
    cls.name_key: cls.get_root().unwrap()
    for cls in _address_types
}


def get_address_cls(name: str) -> Type[Address]:
    return _default_addrs[name]


def is_wrapped_addr(addr: any) -> bool:
    return type(addr) in _address_types


def wrap_address(addr: AddressTypes) -> Address:

    if is_wrapped_addr(addr):
        return addr

    cls = None
    match addr:
        case str():
            cls = UDSAddress

        case tuple() | list():
            cls = TCPAddress

        case None:
            cls = get_address_cls(preferred_transport)
            addr = cls.get_root().unwrap()

        case _:
            raise TypeError(
                f'Can not wrap addr {addr} of type {type(addr)}'
            )

    return cls.from_addr(addr)


def default_lo_addrs(transports: list[str]) -> list[AddressTypes]:
    return [
        _default_lo_addrs[transport]
        for transport in transports
    ]
