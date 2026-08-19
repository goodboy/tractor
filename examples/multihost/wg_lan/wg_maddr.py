# tractor: distributed structured concurrency.
r'''
Verify `wg` peers declared by tractor's multiaddr parser.

`tractor.discovery.parse_wg_maddr()` owns pure parsing and delegates
all tunnel peeling to `py-multiaddr`. This example keeps only the
explicit impure probe used by the two-host demo; parsing never shells
out or verifies local interface state implicitly.

The canonical maddr form is:

    /ip4/10.0.0.1/udp/51820/wg/u<key>/ip4/10.0.11.1/tcp/1616
    \_______ wg bearer ______/\_ key _/\____ tractor ep _____/

The kernel owns the bearer socket. A future tractor bindspace may
provision it through netlink, but only the overlay is an application
`MsgTransport` endpoint.

'''
from __future__ import annotations
import subprocess

from tractor.discovery import (
    TunnelledAddress,
    WGTunnelSpec,
)


def verify_wg_peer(
    addr: TunnelledAddress,
    iface: str|None = None,
) -> bool:
    '''
    Check the outer tunnel's key against one local `wg` iface.

    IMPURE + explicit by design: neither `parse_wg_maddr()` nor
    `tractor.discovery.parse_maddr()` calls this probe.

    ?TODO, per plan-03 layer B, swap this body for `pyroute2`
    while retaining the explicit verification boundary.

    '''
    spec = addr.tunnel
    if not isinstance(spec, WGTunnelSpec):
        raise TypeError(
            f'Unsupported tunnel spec: {type(spec)!r}'
        )

    iface = iface or spec.iface

    def _wg(*args: str) -> str:
        return subprocess.run(
            ['wg', 'show', iface, *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    return (
        spec.peer_pubkey in _wg('peers').split()
        or
        spec.peer_pubkey == _wg('public-key').strip()
    )
