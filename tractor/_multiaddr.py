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
Multiaddress parser and utils according the spec(s) defined by
`libp2p` and used in dependent project such as `ipfs`:

- https://docs.libp2p.io/concepts/fundamentals/addressing/
- https://github.com/libp2p/specs/blob/master/addressing/README.md

'''
from typing import Iterator

from bidict import bidict

# TODO: see if we can leverage libp2p ecosys projects instead of
# rolling our own (parser) impls of the above addressing specs:
# - https://github.com/libp2p/py-libp2p
# - https://docs.libp2p.io/concepts/nat/circuit-relay/#relay-addresses
# prots: bidict[int, str] = bidict({
prots: bidict[int, str] = {
    'ipv4': 3,
    'ipv6': 3,
    'wg': 3,

    'tcp': 4,
    'udp': 4,

    # TODO: support the next-gen shite Bo
    # 'quic': 4,
    # 'ssh': 7,  # via rsyscall bootstrapping
}

prot_params: dict[str, tuple[str]] = {
    'ipv4': ('addr',),
    'ipv6': ('addr',),
    'wg': ('addr', 'port', 'pubkey'),

    'tcp': ('port',),
    'udp': ('port',),

    # 'quic': ('port',),
    # 'ssh': ('port',),
}


def iter_prot_layers(
    multiaddr: str,
) -> Iterator[
    tuple[
        int,
        list[str]
    ]
]:
    '''
    Unpack a libp2p style "multiaddress" into multiple "segments"
    for each "layer" of the protocoll stack (in OSI terms).

    '''
    tokens: list[str] = multiaddr.split('/')
    root, tokens = tokens[0], tokens[1:]
    assert not root  # there is a root '/' on LHS
    itokens = iter(tokens)

    prot: str | None = None
    params: list[str] = []
    for token in itokens:
        # every prot path should start with a known
        # key-str.
        if token in prots:
            if prot is None:
                prot: str = token
            else:
                yield prot, params
                prot = token

            params = []

        elif token not in prots:
            params.append(token)

    else:
        yield prot, params


def parse_maddr(
    multiaddr: str,
) -> dict[str, str | int | dict]:
    '''
    Parse a libp2p style "multiaddress" into it's distinct protocol
    segments where each segment is of the form:

        `../<protocol>/<param0>/<param1>/../<paramN>`

    and is loaded into a (order preserving) `layers: dict[str,
    dict[str, Any]` which holds each protocol-layer-segment of the
    original `str` path as a separate entry according to its approx
    OSI "layer number".

    Any `paramN` in the path must be distinctly defined by a str-token in the
    (module global) `prot_params` table.

    For eg. for wireguard which requires an address, port number and publickey
    the protocol params are specified as the entry:

        'wg': ('addr', 'port', 'pubkey'),

    and are thus parsed from a maddr in that order:
        `'/wg/1.1.1.1/51820/<pubkey>'`

    '''
    layers: dict[str, str | int | dict] = {}
    for (
        prot_key,
        params,
    ) in iter_prot_layers(multiaddr):

        layer: int = prots[prot_key]  # OSI layer used for sorting
        ep: dict[str, int | str] = {'layer': layer}
        layers[prot_key] = ep

        # TODO; validation and resolving of names:
        # - each param via a validator provided as part of the
        #   prot_params def? (also see `"port"` case below..)
        # - do a resolv step that will check addrs against
        #   any loaded network.resolv: dict[str, str]
        rparams: list = list(reversed(params))
        for key in prot_params[prot_key]:
            val: str | int = rparams.pop()

            # TODO: UGHH, dunno what we should do for validation
            # here, put it in the params spec somehow?
            if key == 'port':
                val = int(val)

            ep[key] = val

    return layers
