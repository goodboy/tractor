# tractor: distributed structured concurrency.
r'''
Verify `wg` keys declared by tractor's multiaddr parser.

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
from typing import Literal

import trio

from tractor.discovery import (
    TunnelledAddress,
    WGTunnelSpec,
)


async def verify_wg_key(
    addr: TunnelledAddress,
    role: Literal['local', 'peer'],
    iface: str | None = None,
    timeout: float = 5,
    inspection: str | None = None,
) -> bool:
    '''
    Verify the declared key in the role required on this host.

    A bearer host uses `role='local'`; a dialer uses `role='peer'`.
    This verifies only key presence. It does not inspect the peer's
    endpoint, AllowedIPs, handshake state, or iface addresses.

    `inspection` accepts output captured by a separate privileged
    `wg show` step. Without it, query asynchronously for callers
    which already have interface-inspection permission.

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

    match role:
        case 'local':
            field = 'public-key'
        case 'peer':
            field = 'peers'
        case _:
            raise ValueError(
                f'Unknown WireGuard key role: {role!r}'
            )

    if inspection is None:
        with trio.fail_after(timeout):
            proc = await trio.run_process(
                ['wg', 'show', iface, field],
                capture_stdout=True,
                check=True,
            )
        inspection = proc.stdout.decode()

    if role == 'local':
        return spec.peer_pubkey == inspection.strip()
    return spec.peer_pubkey in inspection.split()
