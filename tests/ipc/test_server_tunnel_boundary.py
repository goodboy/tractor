'''
Tunnel annotation peeling at the inbound IPC transport boundary.

'''
from __future__ import annotations

import trio

from tractor.net import (
    BindspaceRef,
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
    original declaration and realized bindspace ref for
    diagnostics. This retained metadata does not claim the listener
    process entered that namespace.

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
    ref: BindspaceRef = BindspaceRef(
        kind='netns',
        key='actor-net',
        inode=1234,
    )
    declared: TunnelledAddress = tunnelled.with_bindspace_ref(
        ref,
    )

    async def main() -> None:
        async with open_ipc_server() as server:
            eps = await server.listen_on(
                accept_addrs=[declared],
            )
            assert len(eps) == 1
            endpoint = eps[0]

            assert type(endpoint.addr) is TCPAddress
            _, host, port = endpoint.addr.unwrap()
            assert host == overlay.unwrap()[1]
            assert port > 0
            assert endpoint.addr is not declared
            assert endpoint.declared_addr is declared
            namespace: tuple[str, int] = ('netns', 1234)
            assert endpoint.namespace == namespace
            endpoint_repr: str = endpoint.pformat()
            server_repr: str = server.pformat()
            expected_namespace: str = f'namespace: {namespace!r}'
            assert expected_namespace in endpoint_repr
            assert ' |_namespaces:' in server_repr
            assert 'netns' in server_repr
            assert '1234' in server_repr
            assert tunnels_of(declared) == (
                declared.tunnel,
            )

            server.cancel()

    trio.run(main)
