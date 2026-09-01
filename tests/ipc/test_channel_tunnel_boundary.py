'''
Tunnel annotation peeling at the outbound IPC transport boundary.

'''
from __future__ import annotations

import pytest
import trio

from tractor.net import (
    TunnelledAddress,
    WGTunnelSpec,
    tunnels_of,
)
from tractor.ipc import _chan
from tractor.ipc._tcp import TCPAddress


_PUBKEY: str = 'g3x7z0AdV1rM6UQU22CC7IL3/ivn4DzrE7ikDhCZ/Dc='


@pytest.fixture
def overlay() -> TCPAddress:
    return TCPAddress('127.0.0.1', 0)


@pytest.fixture
def tunnelled(
    overlay: TCPAddress,
) -> TunnelledAddress:
    return TunnelledAddress(
        overlay=overlay,
        tunnel=WGTunnelSpec(
            peer_pubkey=_PUBKEY,
            bearer=('192.168.1.50', 51820),
        ),
    )


@pytest.mark.parametrize('use_tunnel', [False, True])
def test_channel_peels_before_transport_dispatch(
    monkeypatch,
    overlay: TCPAddress,
    tunnelled: TunnelledAddress,
    use_tunnel: bool,
):
    '''
    Exact-type transport lookup cannot dispatch a `TunnelledAddress`,
    and passing one onward would make TCP dial the wrong object. Feed
    both a plain overlay and its annotated wrapper into
    `Channel.from_addr()`, capture lookup and connect arguments, and
    prove both transport operations receive only the same bindable
    TCP address while the caller's tunnel metadata remains intact.

    '''
    seen: list[tuple[str, TCPAddress]] = []

    class FakeTransport:
        @classmethod
        async def connect_to(
            cls,
            addr: TCPAddress,
            **kwargs,
        ) -> FakeTransport:
            seen.append(('connect', addr))
            return cls()

    def fake_transport_from_addr(
        addr: TCPAddress,
    ) -> type[FakeTransport]:
        seen.append(('lookup', addr))
        return FakeTransport

    monkeypatch.setattr(
        _chan,
        'transport_from_addr',
        fake_transport_from_addr,
    )

    async def main() -> None:
        declared = tunnelled if use_tunnel else overlay
        chan = await _chan.Channel.from_addr(declared)
        assert isinstance(chan.transport, FakeTransport)

    trio.run(main)

    assert seen == [
        ('lookup', overlay),
        ('connect', overlay),
    ]
    assert tunnels_of(tunnelled) == (tunnelled.tunnel,)
