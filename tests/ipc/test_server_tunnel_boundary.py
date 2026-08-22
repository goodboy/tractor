'''
Tunnel annotation peeling at the inbound IPC transport boundary.

'''
from __future__ import annotations

import trio

from tractor.discovery import (
    TunnelledAddress,
    WGTunnelSpec,
    tunnels_of,
)
from tractor.ipc._server import open_ipc_server
from tractor.ipc._tcp import TCPAddress


_PUBKEY: str = 'g3x7z0AdV1rM6UQU22CC7IL3/ivn4DzrE7ikDhCZ/Dc='


def test_server_peels_before_endpoint_construction() -> None:
    '''
    `Endpoint.start_listener()` reflects on its address's declaring
    module, so retaining a tunnel wrapper there selects `._tunnel`
    instead of the TCP backend. Start a real listener from the
    wrapper, assert the resulting `Endpoint` contains only a resolved
    `TCPAddress`. Prove `Endpoint.declared_addr` still retains the
    original tunnel namespace for diagnostics and the future
    bindspace lifecycle.

    '''
    overlay = TCPAddress('127.0.0.1', 0)
    tunnelled = TunnelledAddress(
        overlay=overlay,
        tunnel=WGTunnelSpec(
            peer_pubkey=_PUBKEY,
            bearer=('192.168.1.50', 51820),
            netns='actor-net',
        ),
    )

    async def main() -> None:
        async with open_ipc_server() as server:
            eps = await server.listen_on(
                accept_addrs=[tunnelled],
            )
            assert len(eps) == 1
            endpoint = eps[0]

            assert type(endpoint.addr) is TCPAddress
            _, host, port = endpoint.addr.unwrap()
            assert host == overlay.unwrap()[1]
            assert port > 0
            assert endpoint.addr is not tunnelled
            assert endpoint.declared_addr is tunnelled
            namespace: tuple[str, str] = ('netns', 'actor-net')
            assert endpoint.namespace == namespace
            endpoint_repr: str = endpoint.pformat()
            server_repr: str = server.pformat()
            expected_namespace: str = f'namespace: {namespace!r}'
            assert expected_namespace in endpoint_repr
            assert ' |_namespaces:' in server_repr
            assert 'netns' in server_repr
            assert 'actor-net' in server_repr
            assert tunnels_of(tunnelled) == (
                tunnelled.tunnel,
            )

            server.cancel()

    trio.run(main)
