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
Multiaddress support using the upstream `py-multiaddr` lib
(a `libp2p` community standard) instead of our own NIH parser.

- https://github.com/multiformats/multiaddr
- https://github.com/multiformats/py-multiaddr
- https://github.com/multiformats/multiaddr/blob/master/protocols.csv
- https://github.com/multiformats/multiaddr/blob/master/protocols/unix.md

'''
import ipaddress
from pathlib import Path
from typing import TYPE_CHECKING

from multiaddr import Multiaddr

if TYPE_CHECKING:
    from tractor.discovery._addr import Address

# map from tractor-internal `proto_key` identifiers
# to the standard multiaddr protocol name strings.
_tpt_proto_to_maddr: dict[str, str] = {
    'tcp': 'tcp',
    'uds': 'unix',
}

# reverse mapping: multiaddr protocol name -> tractor proto_key
_maddr_to_tpt_proto: dict[str, str] = {
    v: k for k, v in _tpt_proto_to_maddr.items()
}
# {'tcp': 'tcp', 'unix': 'uds'}


def mk_maddr(
    addr: 'Address',
) -> Multiaddr:
    '''
    Construct a `Multiaddr` from a tractor `Address` instance,
    dispatching on the `.proto_key` to build the correct
    multiaddr-spec-compliant protocol path.

    '''
    proto_key: str = addr.proto_key
    maddr_proto: str|None = _tpt_proto_to_maddr.get(proto_key)
    if maddr_proto is None:
        raise ValueError(
            f'Unsupported proto_key: {proto_key!r}'
        )

    match proto_key:
        case 'tcp':
            host, port = addr.unwrap()
            ip = ipaddress.ip_address(host)
            net_proto: str = (
                'ip4' if ip.version == 4
                else 'ip6'
            )
            return Multiaddr(
                f'/{net_proto}/{host}/{maddr_proto}/{port}'
            )

        case 'uds':
            filedir, filename = addr.unwrap()
            filepath = Path(filedir) / filename
            # NOTE, strip any leading `/` to avoid
            # double-slash `/unix//run/..` which the
            # multiaddr parser rejects as "empty
            # protocol path".
            fpath_str: str = str(filepath).lstrip('/')
            return Multiaddr(
                f'/{maddr_proto}/{fpath_str}'
            )


def parse_maddr(
    maddr_str: str,
) -> 'Address':
    '''
    Parse a multiaddr string into a tractor `Address`.

    Inverse of `mk_maddr()`.

    '''
    # lazy imports to avoid circular deps
    from tractor.ipc._tcp import TCPAddress
    from tractor.ipc._uds import UDSAddress

    maddr = Multiaddr(maddr_str)
    proto_names: list[str] = [
        p.name for p in maddr.protocols()
    ]

    match proto_names:
        case [('ip4' | 'ip6') as net_proto, 'tcp']:
            host: str = maddr.value_for_protocol(net_proto)
            port: int = int(maddr.value_for_protocol('tcp'))
            return TCPAddress(host, port)

        case ['unix']:
            # NOTE, the multiaddr lib prepends a `/` to the
            # unix protocol value which effectively restores
            # the absolute-path semantics that `mk_maddr()`
            # strips when building the multiaddr string.
            raw: str = maddr.value_for_protocol('unix')
            sockpath = Path(raw)
            return UDSAddress(
                filedir=sockpath.parent,
                filename=sockpath.name,
            )

        case _:
            raise ValueError(
                f'Unsupported multiaddr protocol combo: '
                f'{proto_names!r}\n'
                f'from maddr: {maddr_str!r}\n'
            )


# type aliases for service-endpoint config tables
#
# input table: actor/service name -> list of maddr strings
# or raw unwrapped-address tuples (as accepted by
# `wrap_address()`).
EndpointsTable = dict[
    str,                    # actor/service name
    list[str|tuple],        # maddr strs or UnwrappedAddress
]

# output table: actor/service name -> list of wrapped
# `Address` instances ready for transport binding.
ParsedEndpoints = dict[
    str,                    # actor/service name
    list['Address'],
]


def parse_endpoints(
    service_table: EndpointsTable,
) -> ParsedEndpoints:
    '''
    Parse a service-endpoint config table into wrapped
    `Address` instances suitable for transport binding.

    Each key is an actor/service name and each value is
    a list of addresses in any format accepted by
    `wrap_address()`:

    - multiaddr strings: ``'/ip4/127.0.0.1/tcp/1616'``
    - UDS multiaddr strings using the **multiaddr spec
      name** ``/unix/...`` (NOT the tractor-internal
      ``/uds/`` proto_key)
    - raw unwrapped tuples: ``('127.0.0.1', 1616)``
    - pre-wrapped `Address` objects (passed through)

    Returns a new `dict` with the same keys, where each
    value list contains the corresponding `Address`
    instances.

    Raises `ValueError` for unsupported multiaddr
    protocols (e.g. ``/udp/``).

    '''
    from tractor.discovery._addr import wrap_address

    parsed: ParsedEndpoints = {}
    for (
        actor_name,
        addr_entries,
    ) in service_table.items():
        parsed[actor_name] = [
            wrap_address(entry)
            for entry in addr_entries
        ]
    return parsed
