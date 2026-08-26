'''
WireGuard interface policy and owned lifecycle contracts.

'''
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

import pytest
import trio

from tractor.discovery import (
    BindspaceHandle,
    BindspaceIdentity,
    BindspaceSpec,
    WGInterfaceConfig,
    WGPeerConfig,
    WGTunnelSpec,
    open_wg_iface,
)
from tractor.discovery import _tunnel


_LOCAL_KEY: str = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='
_PEER_KEY: str = 'r1LKM1pqhuY9Z6L4y5jQ2fGX67kJSrq5kRV5Jk2ywEo='


def test_wg_iface_settings_follow_role() -> None:
    '''
    Listen and dial roles interpret the tunnel bearer differently.

    Prove listen derives its local port from the bearer while dial
    applies the bearer as the selected peer's omitted endpoint. Other
    explicit peers retain their own endpoint and routing policy.

    '''
    selected: WGPeerConfig = WGPeerConfig(
        public_key=_PEER_KEY,
        allowed_ips=('10.1.0.0/16',),
    )
    other: WGPeerConfig = WGPeerConfig(
        public_key=_LOCAL_KEY,
        endpoint=('198.51.100.2', 51821),
    )
    config: WGInterfaceConfig = WGInterfaceConfig(
        private_key=_LOCAL_KEY,
        peers=(selected, other),
    )
    spec: WGTunnelSpec = WGTunnelSpec(
        peer_pubkey=_PEER_KEY,
        bearer=('192.0.2.1', 51820),
    )

    listen_port: int|None
    listen_peers: tuple[dict[str, object], ...]
    listen_port, listen_peers = _tunnel._wg_iface_settings(
        spec,
        config,
        'listen',
    )
    assert listen_port == 51820
    # A listen bearer configures the local port, not a peer endpoint.
    assert 'endpoint_addr' not in listen_peers[0]

    dial_port: int|None
    dial_peers: tuple[dict[str, object], ...]
    dial_port, dial_peers = _tunnel._wg_iface_settings(
        spec,
        config,
        'dial',
    )
    # No local dial listen port was declared; the bearer is remote.
    assert dial_port is None
    assert dial_peers[0]['endpoint_addr'] == '192.0.2.1'
    assert dial_peers[0]['endpoint_port'] == 51820
    assert dial_peers[0]['allowed_ips'] == ['10.1.0.0/16']
    assert dial_peers[1]['endpoint_addr'] == '198.51.100.2'
    assert dial_peers[1]['endpoint_port'] == 51821


@pytest.mark.parametrize(
    ('config', 'role', 'match'),
    (
        pytest.param(
            WGInterfaceConfig(
                private_key=_LOCAL_KEY,
            ),
            'dial',
            'not in.*configured peer keys',
            id='missing-dial-peer',
        ),
        pytest.param(
            WGInterfaceConfig(
                private_key=_LOCAL_KEY,
                listen_port=51821,
            ),
            'listen',
            '51821.*51820',
            id='listen-port-51821-vs-bearer-51820',
        ),
        pytest.param(
            WGInterfaceConfig(
                private_key=_LOCAL_KEY,
                peers=(
                    WGPeerConfig(
                        public_key=_PEER_KEY,
                        endpoint=('198.51.100.1', 51820),
                    ),
                ),
            ),
            'dial',
            '198.51.100.1.*192.0.2.1',
            id='dial-endpoint-conflict',
        ),
    ),
)
def test_wg_iface_settings_reject_conflicts(
    config: WGInterfaceConfig,
    role: str,
    match: str,
) -> None:
    '''
    Role-dependent conflicts must fail before pyroute2 side effects.

    '''
    spec: WGTunnelSpec = WGTunnelSpec(
        peer_pubkey=_PEER_KEY,
        bearer=('192.0.2.1', 51820),
    )
    with pytest.raises(ValueError, match=match):
        _tunnel._wg_iface_settings(
            spec,
            config,
            role,  # type: ignore[arg-type]
        )


def test_open_wg_iface_shields_cancelled_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    '''
    Cancellation after creation must still remove the owned WG iface.

    Pin a stand-in namespace FD and fake privileged create/remove
    calls. Cancel inside the yielded context and prove shielded
    teardown runs before cancellation leaves the enclosing scope.

    '''
    token_path: Path = tmp_path / 'netns'
    token_path.touch()
    events: list[str] = []

    def create(
        spec: WGTunnelSpec,
        config: WGInterfaceConfig,
        bindspace: BindspaceHandle,
        listen_port: int|None,
        peers: tuple[dict[str, object], ...],
    ) -> None:
        '''
        Record the validated create request.

        '''
        assert bindspace.namespace_fd is not None
        assert listen_port is None
        assert peers[0]['public_key'] == _PEER_KEY
        events.append('create')

    def remove(
        spec: WGTunnelSpec,
        bindspace: BindspaceHandle,
    ) -> None:
        '''
        Record shielded removal after cancellation.

        '''
        events.append('remove')

    monkeypatch.setattr(
        _tunnel,
        '_sync_create_wg_iface',
        create,
    )
    monkeypatch.setattr(
        _tunnel,
        '_sync_remove_wg_iface',
        remove,
    )

    namespace_file: BinaryIO
    with token_path.open('rb') as namespace_file:
        namespace_fd: int = namespace_file.fileno()
        bindspace_spec: BindspaceSpec = BindspaceSpec(
            kind='netns',
            key='tractor-wg0',
        )
        bindspace: BindspaceHandle = BindspaceHandle(
            spec=bindspace_spec,
            identity=BindspaceIdentity(
                kind='netns',
                key='tractor-wg0',
                inode=os.fstat(namespace_fd).st_ino,
            ),
            namespace_fd=namespace_fd,
            ownership='borrowed',
        )
        peer: WGPeerConfig = WGPeerConfig(
            public_key=_PEER_KEY,
        )
        config: WGInterfaceConfig = WGInterfaceConfig(
            private_key=_LOCAL_KEY,
            peers=(peer,),
        )
        spec: WGTunnelSpec = WGTunnelSpec(
            peer_pubkey=_PEER_KEY,
        )

        async def main() -> None:
            '''
            Cancel while the fake WG iface is owned.

            '''
            with trio.CancelScope() as scope:
                async with open_wg_iface(
                    spec,
                    config,
                    bindspace,
                    'dial',
                ):
                    events.append('yield')
                    scope.cancel()
                    await trio.sleep_forever()

        trio.run(main)

    assert events == ['create', 'yield', 'remove']
