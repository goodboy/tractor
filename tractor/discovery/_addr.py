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
from typing import TypeAlias

from ..log import get_logger
from ..runtime._state import (
    _def_tpt_proto,
)
from ..ipc._tcp import TCPAddress
from ..ipc._uds import (
    UDSAddress,
    HAS_UDS,
)
from .types import (
    Address as Address,
    AddressDeclaration,
    LegacyUnwrappedAddress,
    TaggedAddress,
    TaggedUDSAlias,
    UnwrappedAddress,
)

log = get_logger()

_AddressType: TypeAlias = type[TCPAddress]|type[UDSAddress]

# ?TODO? should we also include another 2 fields from our `Aid` msg
# such that we include the runtime `Actor.uid` of `.name` and `.uuid`?
# - would ensure uniqueness across entire net?
# - allows for easier runtime-level filtering of "actors by service
#   name"


# the address types available on this host: TCP always, UDS only
# where usable (`HAS_UDS`). Both registries derive from this single
# list via each type's `proto_key`.
_address_protos: list[_AddressType] = [TCPAddress]
if HAS_UDS:
    _address_protos.append(UDSAddress)

_address_types: dict[str, _AddressType] = {
    cls.proto_key: cls
    for cls in _address_protos
}


# TODO! really these are discovery sys default addrs ONLY useful for
# when none is provided to a root actor on first boot.
#
# TODO, this should be something like a `.get_def_registar_addr()`
# or similar since,
# - it should be a **host singleton** (not root/tree singleton)
# - we **only need this value** when one isn't provided to the
#   runtime at boot and we want to implicitly provide a host-wide
#   registrar.
# - each rooted-actor-tree should likely have its own
#   micro-registry (likely the root being it), also see
_default_lo_addrs: dict[str, UnwrappedAddress] = {
    cls.proto_key: cls.get_root().unwrap()
    for cls in _address_protos
}


def get_address_cls(name: str) -> _AddressType:
    try:
        return _address_types[name]
    except KeyError:
        raise NotImplementedError(
            f'No IPC transport backend for {name!r} on this '
            f'platform!\n'
            f'(available: {list(_address_types)})\n'
        )


def is_wrapped_addr(addr: object) -> bool:
    # XXX NOTE, a `TunnelledAddress` is genuinely "wrapped" but is
    # deliberately NOT in `_address_types`: it has no
    # `MsgTransport` of its own (a tunnel is transparent to
    # `socket(2)`), so it gets no proto-key entry. See
    # `tractor.net._tunnel`.
    from tractor.net._tunnel import TunnelledAddress
    return (
        type(addr) in _address_types.values()
        or
        isinstance(addr, TunnelledAddress)
    )


def mk_uuid() -> str:
    '''
    Encapsulate creation of a uuid4 as `str` as used
    for creating `Actor.uid: tuple[str, str]` and/or
    `.msg.types.Aid`.

    '''
    return str(uuid4())


def wrap_address(
    addr: (
        TaggedAddress
        |TaggedUDSAlias
        |LegacyUnwrappedAddress
        |list[str|int]
        |str
        |AddressDeclaration
    ),
) -> AddressDeclaration:
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

    cls: _AddressType|None = None
    # if 'sock' in addr[0]:
    #     import pdbp; pdbp.set_trace()
    match addr:

        case (
            ('tcp', str(), int())
            |
            ['tcp', str(), int()]
        ):
            return TCPAddress.from_addr(addr)

        case (
            (('unix' | 'uds'), str())
            |
            [('unix' | 'uds'), str()]
        ):
            return UDSAddress.from_addr(addr)

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
            cls = get_address_cls(_def_tpt_proto)
            addr: UnwrappedAddress = cls.get_root().unwrap()

        # multiaddr-format string, e.g.
        # '/ip4/127.0.0.1/tcp/1616'
        case str() if addr.startswith('/'):
            from tractor.net import (
                parse_maddr,
            )
            return parse_maddr(addr)

        case _:
            # import pdbp; pdbp.set_trace()
            # from tractor.devx import mk_pdb
            # mk_pdb().set_trace()
            raise TypeError(
                f'Can not wrap unwrapped-address ??\n'
                f'type(addr): {type(addr)!r}\n'
                f'addr: {addr!r}\n'
            )

    return cls.from_addr(addr)


def default_lo_addrs(
    transports: list[str],
) -> list[UnwrappedAddress]:
    '''
    Return the default, host-singleton, registry address
    for an input transport key set.

    '''
    lo_addrs: list[UnwrappedAddress] = []
    for transport in transports:
        try:
            lo_addrs.append(_default_lo_addrs[transport])
        except KeyError:
            raise NotImplementedError(
                f'No default loopback addr for transport '
                f'{transport!r} on this platform!\n'
                f'(available: {list(_default_lo_addrs)})\n'
            )
    return lo_addrs
