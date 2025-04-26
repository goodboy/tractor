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
from uuid import uuid4
from typing import (
    Protocol,
    ClassVar,
    Type,
    TYPE_CHECKING,
)

from bidict import bidict
from trio import (
    SocketListener,
)

from .log import get_logger
from ._state import (
    _def_tpt_proto,
)
from .ipc._tcp import TCPAddress
from .ipc._uds import UDSAddress

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

        # classic network socket-address as tuple/list
        case (
            (str(), int())
            |
            [str(), int()]
        ):
            cls = TCPAddress

        case (
            # (str()|Path(), str()|Path()),
            # ^TODO? uhh why doesn't this work!?

            (_, filename)
        ) if type(filename) is str:
            cls = UDSAddress

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
                f'Can not wrap unwrapped-address ??\n'
                f'type(addr): {type(addr)!r}\n'
                f'addr: {addr!r}\n'
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
