# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or
# modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.

'''
Dependency-neutral address typing declarations.

Keep this module independent of concrete IPC transports, actor
runtime models, network resources, tunnels, and optional
dependencies.

'''
from __future__ import annotations

from pathlib import Path
from typing import (
    ClassVar,
    Literal,
    Protocol,
    TypeAlias,
)


# TODO, maybe breakout the netns key to a struct?
# class NetNs(Struct)[str, int]:
#     ...

# TODO, can't we just use a type alias
# for this? namely just some `tuple[str, int, str, str]`?
#
# -[ ] would also just be simpler to keep this as
#     SockAddr[tuple] or something, implying it's just a simple pair
#     of values which can presumably be mapped to all transports?
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
#    seems like the right name as per the GeeksForGeeks article,
#    "Introduction to Address Descriptor".
#
TaggedTCPAddress: TypeAlias = tuple[
    Literal['tcp'],
    str,
    int,
]
TaggedUnixAddress: TypeAlias = tuple[
    Literal['unix'],
    str,
]
TaggedUDSAlias: TypeAlias = tuple[
    Literal['uds'],
    str,
]
TaggedAddress: TypeAlias = (
    TaggedTCPAddress
    |TaggedUnixAddress
)

# Input-only compatibility forms retained for older callers and
# serialized payloads.
LegacyTCPAddress: TypeAlias = tuple[str, int]
LegacyUDSAddress: TypeAlias = tuple[str, str]
LegacyUnwrappedAddress: TypeAlias = (
    LegacyTCPAddress
    |LegacyUDSAddress
)
UnwrappedAddress = TaggedAddress


class AddressDeclaration(Protocol):
    '''
    Common instance shape of plain and tunnelled addresses.

    This protocol deliberately excludes transport registration and
    listener operations. A tunnel declaration delegates these address
    properties to its innermost concrete transport address.

    '''
    @property
    def proto_key(self) -> str:
        ...

    @property
    def is_valid(self) -> bool:
        ...

    @property
    def namespace(self) -> tuple[str, str|int]|None:
        ...

    @property
    def bindspace(self) -> str|Path:
        ...

    def unwrap(self) -> UnwrappedAddress:
        ...


# TODO, maybe rename to `SocketAddress`?
class Address(Protocol):
    '''
    Concrete address contract used by IPC transport backends.

    Unlike `AddressDeclaration`, transport registries require
    class-level protocol metadata. Transport-specific factories stay
    on their concrete address classes because their descriptor and
    bindspace inputs differ.

    '''
    proto_key: ClassVar[str]
    unwrapped_type: ClassVar[type]

    # TODO, i feel like an `.is_bound()` is a better thing to
    # support?
    # Lke, what use does this have besides a noop and if it's not
    # valid why aren't we erroring on creation/use?
    @property
    def is_valid(self) -> bool:
        ...

    # TODO, maybe `.netns` is a better name?
    @property
    def namespace(self) -> tuple[str, str|int]|None:
        '''
        The if-available, OS-specific "network namespace" key.

        '''
        ...

    @property
    def bindspace(self) -> str|Path:
        '''
        Deliver the address' transport-specific bindable space.

        '''
        ...

    def unwrap(self) -> UnwrappedAddress:
        '''
        Deliver the underlying primitive address descriptor.

        '''
        ...

__all__ = (
    'Address',
    'AddressDeclaration',
    'LegacyTCPAddress',
    'LegacyUDSAddress',
    'LegacyUnwrappedAddress',
    'TaggedAddress',
    'TaggedTCPAddress',
    'TaggedUDSAlias',
    'TaggedUnixAddress',
    'UnwrappedAddress',
)
