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
            return Multiaddr(
                f'/{maddr_proto}/{filepath}'
            )
