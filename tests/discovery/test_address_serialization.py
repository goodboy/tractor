'''
Canonical tagged-address decoding and legacy input compatibility.

'''
from pathlib import Path
from types import SimpleNamespace

import pytest
import trio

from tractor.discovery._addr import wrap_address
from tractor.discovery._registry import Registrar
from tractor.ipc._tcp import TCPAddress
from tractor.ipc._uds import UDSAddress


@pytest.mark.parametrize(
    'value',
    [
        ('tcp', '127.0.0.1', 1616),
        ['tcp', '127.0.0.1', 1616],
    ],
)
def test_decode_tagged_tcp_address(value):
    '''
    Shape-only decoding cannot distinguish future transport address
    forms. Feed canonical tuple and msgpack-style list values through
    the compatibility boundary and prove the explicit `tcp` tag
    selects the TCP backend and emits the canonical tagged form.

    '''
    addr = wrap_address(value)

    assert type(addr) is TCPAddress
    assert addr.unwrap() == ('tcp', '127.0.0.1', 1616)


@pytest.mark.parametrize(
    'tag',
    ['unix', 'uds'],
)
@pytest.mark.parametrize('container', [tuple, list])
def test_decode_tagged_unix_address(
    tag: str,
    container: type,
):
    '''
    Multiaddr calls the protocol `unix` while tractor's transport key
    remains `uds`. Decode both spellings from tuple/list containers,
    normalize them to one `UDSAddress`, and emit the canonical `unix`
    spelling.

    '''
    value = container((tag, '/tmp/tractor/registry.sock'))
    addr = wrap_address(value)

    assert type(addr) is UDSAddress
    assert addr.sockpath == Path('/tmp/tractor/registry.sock')
    assert addr.unwrap() == (
        'unix',
        '/tmp/tractor/registry.sock',
    )


@pytest.mark.parametrize(
    'value, expected_type',
    [
        (('127.0.0.1', 1616), TCPAddress),
        (['127.0.0.1', 1616], TCPAddress),
        (('/tmp/tractor', 'registry.sock'), UDSAddress),
        (['/tmp/tractor', 'registry.sock'], UDSAddress),
    ],
)
def test_decode_legacy_address_forms(
    value,
    expected_type: type,
):
    '''
    Existing callers, config, and older msgpack payloads still
    provide untagged pairs. Keep tuple/list forms readable while
    canonical tagged emission is introduced, proving the writer
    migration does not break shipped input behavior.

    '''
    addr = wrap_address(value)

    assert type(addr) is expected_type
    assert addr.unwrap()[0] in {'tcp', 'unix'}


def test_tcp_from_native_ipv6_sockname():
    '''
    `socket.getsockname()` returns a four-item IPv6 sockaddr which is
    neither a wire form nor a legacy two-item pair. Preserve it as an
    OS compatibility boundary and intentionally ignore unsupported
    flow-info/scope-id fields when constructing `TCPAddress`.

    '''
    addr = TCPAddress.from_addr(
        ('::1', 1616, 0, 0)
    )

    assert addr.unwrap() == ('tcp', '::1', 1616)


@pytest.mark.parametrize(
    'legacy, canonical',
    [
        (
            ('127.0.0.1', 1616),
            ('tcp', '127.0.0.1', 1616),
        ),
        (
            ('/tmp/tractor', 'registry.sock'),
            ('unix', '/tmp/tractor/registry.sock'),
        ),
    ],
)
def test_registrar_stores_canonical_addresses(
    legacy: tuple,
    canonical: tuple,
):
    '''
    Normalize registrar entries before stale-address eviction.

    During the tagged-address migration an older actor can register
    an untagged address before a newer actor reuses that endpoint with
    its canonical tag. Store the first declaration canonically, then
    register the tagged spelling under another uid. The old uid must
    be evicted and the registry must retain exactly one canonical
    address for the replacement actor.

    '''
    registrar = SimpleNamespace(
        _registry={},
        _waiters={},
    )
    old_uid = ('old', 'old-uid')
    new_uid = ('new', 'new-uid')

    async def register_both():
        await Registrar.register_actor(
            registrar,
            old_uid,
            legacy,
        )
        assert registrar._registry[old_uid] == [canonical]

        await Registrar.register_actor(
            registrar,
            new_uid,
            canonical,
        )

    trio.run(register_both)

    assert registrar._registry == {
        new_uid: [canonical],
    }
