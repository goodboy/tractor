'''
`TunnelledAddress` delegation + peeling semantics.

A tunnel annotates an existing L4 addr rather than being its own
transport, so the contract under test is mostly *delegation*: the
runtime must not be able to tell a tunnelled addr from its
overlay, and **nothing** about the tunnel may cross the wire.

See `ai/tpt-backends/03_wg_tunnel_bindspace.md` §3.1/§3.4.

'''
from __future__ import annotations

import msgspec
import pytest

from tractor.net import (
    BindspaceRef,
    TunnelledAddress,
    WGTunnelSpec,
    mb_pubkey,
    strip_tunnels,
    tunnels_of,
    wg8_pubkey,
)
from tractor.discovery._addr import (
    is_wrapped_addr,
    wrap_address,
)
from tractor.ipc._tcp import TCPAddress
from tractor.ipc._uds import UDSAddress


# a valid-looking std-base64 `wg(8)` pubkey (32B -> 44 chars)
_PUBKEY: str = 'g3x7z0AdV1rM6UQU22CC7IL3/ivn4DzrE7ikDhCZ/Dc='


@pytest.fixture
def overlay() -> TCPAddress:
    return TCPAddress('10.0.11.1', 1616)


@pytest.fixture
def spec() -> WGTunnelSpec:
    return WGTunnelSpec(
        peer_pubkey=_PUBKEY,
        bearer=('192.168.1.50', 51820),
    )


@pytest.fixture
def tunnelled(
    overlay: TCPAddress,
    spec: WGTunnelSpec,
) -> TunnelledAddress:
    return TunnelledAddress(overlay=overlay, tunnel=spec)


def test_wg_pubkey_codec_roundtrip():
    '''
    Standard `wg(8)` base64 keys can contain `/`, which cannot be
    embedded unchanged in a slash-delimited maddr. Prove the helper
    emits `u`-prefixed multibase base64url without `/` and decodes
    it back to the exact original 32-byte key.

    '''
    mb_key: str = mb_pubkey(_PUBKEY)

    assert mb_key.startswith('u')
    assert '/' not in mb_key
    assert wg8_pubkey(mb_key) == _PUBKEY


@pytest.mark.parametrize(
    'key, converter',
    [
        pytest.param(
            'dG9vIHNob3J0',
            mb_pubkey,
            id='wg8-base64',
        ),
        pytest.param(
            'udG9vIHNob3J0',
            wg8_pubkey,
            id='multibase',
        ),
    ],
)
def test_wg_pubkey_codec_rejects_wrong_size(
    key: str,
    converter,
):
    '''
    WireGuard silently-corrupt key handling would let an invalid
    identity reach peer verification. Exercise both input encodings
    with a short payload and prove conversion rejects it before a
    tunnel spec or maddr can be constructed.

    '''
    with pytest.raises(
        ValueError,
        match='must decode to 32 bytes',
    ):
        converter(key)


def test_proto_key_delegates(
    tunnelled: TunnelledAddress,
    overlay: TCPAddress,
):
    '''
    A tunnel has no transport of its own, so every table lookup
    must see the *overlay's* proto-key.

    '''
    assert tunnelled.proto_key == overlay.proto_key == 'tcp'


def test_unwrap_is_identical_to_overlay(
    tunnelled: TunnelledAddress,
    overlay: TCPAddress,
):
    '''
    The whole point: nothing new crosses the wire, so a peer
    never has to understand tunnels.

    '''
    assert tunnelled.unwrap() == overlay.unwrap()

    # and it must survive msgpack as-is
    enc: bytes = msgspec.msgpack.encode(tunnelled.unwrap())
    assert msgspec.msgpack.decode(enc) == list(overlay.unwrap())


def test_unwrap_roundtrips_back_to_plain_overlay(
    tunnelled: TunnelledAddress,
    overlay: TCPAddress,
):
    '''
    `wrap_address()` on a tunnelled addr's unwrapped form yields
    the *plain* overlay type — the tunnel is simply absent, which
    is correct: it was never on the wire.

    '''
    rewrapped = wrap_address(tunnelled.unwrap())
    assert type(rewrapped) is TCPAddress
    assert rewrapped == overlay
    assert not isinstance(rewrapped, TunnelledAddress)


def test_bindspace_and_validity_delegate(
    tunnelled: TunnelledAddress,
    overlay: TCPAddress,
):
    assert tunnelled.bindspace == overlay.bindspace
    assert tunnelled.is_valid == overlay.is_valid


def test_is_wrapped_addr_accepts_tunnelled(
    tunnelled: TunnelledAddress,
    overlay: TCPAddress,
):
    '''
    `TunnelledAddress` is deliberately absent from
    `_address_types`, so `is_wrapped_addr()` needs its own
    clause.

    '''
    assert is_wrapped_addr(overlay)
    assert is_wrapped_addr(tunnelled)
    # the unwrapped form is NOT a wrapped addr
    assert not is_wrapped_addr(tunnelled.unwrap())


def test_namespace_comes_from_the_tunnel(
    overlay: TCPAddress,
):
    '''
    Plain transport addresses explicitly select no namespace, while
    a tunnel can select one for the same concrete overlay.

    '''
    uds_addr: UDSAddress = UDSAddress('/tmp', 'tractor-test.sock')
    assert overlay.namespace is None
    assert uds_addr.namespace is None

    no_ns = TunnelledAddress(
        overlay=overlay,
        tunnel=WGTunnelSpec(peer_pubkey=_PUBKEY),
    )
    assert no_ns.namespace is None

    in_ns = TunnelledAddress(
        overlay=overlay,
        tunnel=WGTunnelSpec(peer_pubkey=_PUBKEY, netns='wg-test'),
    )
    assert in_ns.namespace == ('netns', 'wg-test')


def test_realized_namespace_uses_stable_ref(
    overlay: TCPAddress,
) -> None:
    '''
    Realization must retain a stable ref without mutating the maddr.

    Build an unrealized named declaration, annotate it with the
    matching realized key and inode, and prove the frozen original is
    unchanged. The annotated copy must preserve transport delegation
    and expose the stable inode through `.namespace`. Direct msgspec
    encoding also proves only serializable ref metadata was retained.

    '''
    declared: TunnelledAddress = TunnelledAddress(
        overlay=overlay,
        tunnel=WGTunnelSpec(
            peer_pubkey=_PUBKEY,
            netns='wg-test',
        ),
    )
    ref: BindspaceRef = BindspaceRef(
        kind='netns',
        key='wg-test',
        inode=1234,
    )
    realized: TunnelledAddress = declared.with_bindspace_ref(
        ref,
    )

    assert declared.bindspace_ref is None
    assert realized.bindspace_ref is ref
    assert realized.namespace == ('netns', 1234)
    assert realized.overlay is declared.overlay
    assert realized.tunnel is declared.tunnel
    assert realized.unwrap() == declared.unwrap()
    assert realized.bindspace == declared.bindspace

    declared_payload: dict[str, object] = msgspec.msgpack.decode(
        msgspec.msgpack.encode(declared)
    )
    assert 'bindspace_ref' not in declared_payload

    decoded: dict[str, object] = msgspec.msgpack.decode(
        msgspec.msgpack.encode(realized)
    )
    assert decoded['bindspace_ref'] == {
        'kind': 'netns',
        'key': 'wg-test',
        'inode': 1234,
    }

    mismatched: BindspaceRef = BindspaceRef(
        kind='netns',
        key='other-netns',
        inode=5678,
    )
    with pytest.raises(
        ValueError,
        match='wg-test.*other-netns',
    ):
        declared.with_bindspace_ref(
            mismatched,
        )


def test_strip_tunnels(
    tunnelled: TunnelledAddress,
    overlay: TCPAddress,
    spec: WGTunnelSpec,
):
    # idempotent on a plain addr
    assert strip_tunnels(overlay) is overlay
    # peels one
    assert strip_tunnels(tunnelled) is overlay
    # and collapses a nested stack in one call
    nested = TunnelledAddress(overlay=tunnelled, tunnel=spec)
    assert strip_tunnels(nested) is overlay


def test_tunnels_of_is_outermost_first(
    tunnelled: TunnelledAddress,
    overlay: TCPAddress,
):
    assert tunnels_of(overlay) == ()
    assert tunnels_of(tunnelled) == (tunnelled.tunnel,)

    inner_spec = WGTunnelSpec(peer_pubkey=_PUBKEY, iface='wg1')
    nested = TunnelledAddress(
        overlay=tunnelled,
        tunnel=inner_spec,
    )
    assert tunnels_of(nested) == (inner_spec, tunnelled.tunnel)


def test_frozen(
    tunnelled: TunnelledAddress,
):
    with pytest.raises(AttributeError):
        tunnelled.overlay = TCPAddress('127.0.0.1', 1)
