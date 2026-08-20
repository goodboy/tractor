'''
Canonical tagged-address decoding and legacy input compatibility.

'''
from pathlib import Path

import pytest

from tractor.discovery._addr import wrap_address
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
    selects the TCP backend without changing legacy emission yet.

    '''
    addr = wrap_address(value)

    assert type(addr) is TCPAddress
    assert addr.unwrap() == ('127.0.0.1', 1616)


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
    normalize them to one `UDSAddress`, and retain legacy pair
    emission until the writer migration lands.

    '''
    value = container((tag, '/tmp/tractor/registry.sock'))
    addr = wrap_address(value)

    assert type(addr) is UDSAddress
    assert addr.sockpath == Path('/tmp/tractor/registry.sock')
    assert addr.unwrap() == (
        '/tmp/tractor',
        'registry.sock',
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
    canonical tagged decoding is introduced, proving this
    reader-first commit does not break shipped input behavior.

    '''
    assert type(wrap_address(value)) is expected_type


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

    assert addr.unwrap() == ('::1', 1616)
