'''
Multiaddr construction, parsing, and round-trip tests for
`tractor.discovery._multiaddr.mk_maddr()` and
`tractor.discovery._multiaddr.parse_maddr()`.

'''
from pathlib import Path
from types import SimpleNamespace

import pytest
from multiaddr import Multiaddr

from tractor.ipc._tcp import TCPAddress
from tractor.ipc._uds import UDSAddress
from tractor.discovery._multiaddr import (
    mk_maddr,
    parse_maddr,
    parse_endpoints,
    _tpt_proto_to_maddr,
    _maddr_to_tpt_proto,
)
from tractor.discovery._addr import wrap_address


def test_tpt_proto_to_maddr_mapping():
    '''
    `_tpt_proto_to_maddr` maps all supported `proto_key`
    values to their correct multiaddr protocol names.

    '''
    assert _tpt_proto_to_maddr['tcp'] == 'tcp'
    assert _tpt_proto_to_maddr['uds'] == 'unix'
    assert len(_tpt_proto_to_maddr) == 2


def test_mk_maddr_tcp_ipv4():
    '''
    `mk_maddr()` on a `TCPAddress` with an IPv4 host
    produces the correct `/ip4/<host>/tcp/<port>` multiaddr.

    '''
    addr = TCPAddress('127.0.0.1', 1234)
    result: Multiaddr = mk_maddr(addr)

    assert isinstance(result, Multiaddr)
    assert str(result) == '/ip4/127.0.0.1/tcp/1234'

    protos = result.protocols()
    assert protos[0].name == 'ip4'
    assert protos[1].name == 'tcp'

    assert result.value_for_protocol('ip4') == '127.0.0.1'
    assert result.value_for_protocol('tcp') == '1234'


def test_mk_maddr_tcp_ipv6():
    '''
    `mk_maddr()` on a `TCPAddress` with an IPv6 host
    produces the correct `/ip6/<host>/tcp/<port>` multiaddr.

    '''
    addr = TCPAddress('::1', 5678)
    result: Multiaddr = mk_maddr(addr)

    assert str(result) == '/ip6/::1/tcp/5678'

    protos = result.protocols()
    assert protos[0].name == 'ip6'
    assert protos[1].name == 'tcp'


def test_mk_maddr_uds():
    '''
    `mk_maddr()` on a `UDSAddress` produces a `/unix/<path>`
    multiaddr containing the full socket path.

    '''
    # NOTE, use an absolute `filedir` to match real runtime
    # UDS paths; `mk_maddr()` strips the leading `/` to avoid
    # the double-slash `/unix//run/..` that py-multiaddr
    # rejects as "empty protocol path".
    filedir = '/tmp/tractor_test'
    filename = 'test_sock.sock'
    addr = UDSAddress(
        filedir=filedir,
        filename=filename,
    )
    result: Multiaddr = mk_maddr(addr)

    assert isinstance(result, Multiaddr)

    result_str: str = str(result)
    assert result_str.startswith('/unix/')
    # verify the leading `/` was stripped to avoid double-slash
    assert '/unix/tmp/tractor_test/' in result_str

    sockpath_rel: str = str(
        Path(filedir) / filename
    ).lstrip('/')
    unix_val: str = result.value_for_protocol('unix')
    assert unix_val.endswith(sockpath_rel)


def test_mk_maddr_unsupported_proto_key():
    '''
    `mk_maddr()` raises `ValueError` for an unsupported
    `proto_key`.

    '''
    fake_addr = SimpleNamespace(proto_key='quic')
    with pytest.raises(
        ValueError,
        match='Unsupported proto_key',
    ):
        mk_maddr(fake_addr)


@pytest.mark.parametrize(
    'addr',
    [
        pytest.param(
            TCPAddress('127.0.0.1', 9999),
            id='tcp-ipv4',
        ),
        pytest.param(
            UDSAddress(
                filedir='/tmp/tractor_rt',
                filename='roundtrip.sock',
            ),
            id='uds',
        ),
    ],
)
def test_mk_maddr_roundtrip(addr):
    '''
    `mk_maddr()` output is valid multiaddr syntax that the
    library can re-parse back into an equivalent `Multiaddr`.

    '''
    maddr: Multiaddr = mk_maddr(addr)
    reparsed = Multiaddr(str(maddr))

    assert reparsed == maddr
    assert str(reparsed) == str(maddr)


# ------ parse_maddr() tests ------

def test_maddr_to_tpt_proto_mapping():
    '''
    `_maddr_to_tpt_proto` is the exact inverse of
    `_tpt_proto_to_maddr`.

    '''
    assert _maddr_to_tpt_proto == {
        'tcp': 'tcp',
        'unix': 'uds',
    }


def test_parse_maddr_tcp_ipv4():
    '''
    `parse_maddr()` on an IPv4 TCP multiaddr string
    produce a `TCPAddress` with the correct host and port.

    '''
    result = parse_maddr('/ip4/127.0.0.1/tcp/1234')

    assert isinstance(result, TCPAddress)
    assert result.unwrap() == ('127.0.0.1', 1234)


def test_parse_maddr_tcp_ipv6():
    '''
    `parse_maddr()` on an IPv6 TCP multiaddr string
    produce a `TCPAddress` with the correct host and port.

    '''
    result = parse_maddr('/ip6/::1/tcp/5678')

    assert isinstance(result, TCPAddress)
    assert result.unwrap() == ('::1', 5678)


def test_parse_maddr_uds():
    '''
    `parse_maddr()` on a `/unix/...` multiaddr string
    produce a `UDSAddress` with the correct dir and filename,
    preserving absolute path semantics.

    '''
    result = parse_maddr('/unix/tmp/tractor_test/test.sock')

    assert isinstance(result, UDSAddress)
    filedir, filename = result.unwrap()
    assert filename == 'test.sock'
    assert str(filedir) == '/tmp/tractor_test'


def test_parse_maddr_unsupported():
    '''
    `parse_maddr()` raise `ValueError` for an unsupported
    protocol combination like UDP.

    '''
    with pytest.raises(
        ValueError,
        match='Unsupported multiaddr protocol combo',
    ):
        parse_maddr('/ip4/127.0.0.1/udp/1234')


@pytest.mark.parametrize(
    'addr',
    [
        pytest.param(
            TCPAddress('127.0.0.1', 9999),
            id='tcp-ipv4',
        ),
        pytest.param(
            UDSAddress(
                filedir='/tmp/tractor_rt',
                filename='roundtrip.sock',
            ),
            id='uds',
        ),
    ],
)
def test_parse_maddr_roundtrip(addr):
    '''
    Full round-trip: `addr -> mk_maddr -> str -> parse_maddr`
    produce an `Address` whose `.unwrap()` matches the original.

    '''
    maddr: Multiaddr = mk_maddr(addr)
    maddr_str: str = str(maddr)
    parsed = parse_maddr(maddr_str)

    assert type(parsed) is type(addr)
    assert parsed.unwrap() == addr.unwrap()


def test_wrap_address_maddr_str():
    '''
    `wrap_address()` accept a multiaddr-format string and
    return the correct `Address` type.

    '''
    result = wrap_address('/ip4/127.0.0.1/tcp/9999')

    assert isinstance(result, TCPAddress)
    assert result.unwrap() == ('127.0.0.1', 9999)


# ------ parse_endpoints() tests ------

def test_parse_endpoints_tcp_only():
    '''
    `parse_endpoints()` with a single TCP maddr per actor
    produce the correct `TCPAddress` instances.

    '''
    table = {
        'registry': ['/ip4/127.0.0.1/tcp/1616'],
        'data_feed': ['/ip4/0.0.0.0/tcp/5555'],
    }
    result = parse_endpoints(table)

    assert set(result.keys()) == {'registry', 'data_feed'}

    reg_addr = result['registry'][0]
    assert isinstance(reg_addr, TCPAddress)
    assert reg_addr.unwrap() == ('127.0.0.1', 1616)

    feed_addr = result['data_feed'][0]
    assert isinstance(feed_addr, TCPAddress)
    assert feed_addr.unwrap() == ('0.0.0.0', 5555)


def test_parse_endpoints_mixed_tpts():
    '''
    `parse_endpoints()` with both TCP and UDS maddrs for
    the same actor produce the correct mixed `Address` list.

    '''
    table = {
        'broker': [
            '/ip4/127.0.0.1/tcp/4040',
            '/unix/tmp/tractor/broker.sock',
        ],
    }
    result = parse_endpoints(table)
    addrs = result['broker']

    assert len(addrs) == 2
    assert isinstance(addrs[0], TCPAddress)
    assert addrs[0].unwrap() == ('127.0.0.1', 4040)

    assert isinstance(addrs[1], UDSAddress)
    filedir, filename = addrs[1].unwrap()
    assert filename == 'broker.sock'
    assert str(filedir) == '/tmp/tractor'


def test_parse_endpoints_unwrapped_tuples():
    '''
    `parse_endpoints()` accept raw `(host, port)` tuples
    and wrap them as `TCPAddress`.

    '''
    table = {
        'ems': [('127.0.0.1', 6666)],
    }
    result = parse_endpoints(table)

    addr = result['ems'][0]
    assert isinstance(addr, TCPAddress)
    assert addr.unwrap() == ('127.0.0.1', 6666)


def test_parse_endpoints_mixed_str_and_tuple():
    '''
    `parse_endpoints()` accept a mix of maddr strings and
    raw tuples in the same actor entry list.

    '''
    table = {
        'quoter': [
            '/ip4/127.0.0.1/tcp/7777',
            ('127.0.0.1', 8888),
        ],
    }
    result = parse_endpoints(table)
    addrs = result['quoter']

    assert len(addrs) == 2
    assert isinstance(addrs[0], TCPAddress)
    assert addrs[0].unwrap() == ('127.0.0.1', 7777)

    assert isinstance(addrs[1], TCPAddress)
    assert addrs[1].unwrap() == ('127.0.0.1', 8888)


def test_parse_endpoints_unsupported_proto():
    '''
    `parse_endpoints()` raise `ValueError` when a maddr
    string uses an unsupported protocol like `/udp/`.

    '''
    table = {
        'bad_actor': ['/ip4/127.0.0.1/udp/9999'],
    }
    with pytest.raises(
        ValueError,
        match='Unsupported multiaddr protocol combo',
    ):
        parse_endpoints(table)


def test_parse_endpoints_empty_table():
    '''
    `parse_endpoints()` on an empty table return an empty
    dict.

    '''
    assert parse_endpoints({}) == {}


def test_parse_endpoints_empty_actor_list():
    '''
    `parse_endpoints()` with an actor mapped to an empty
    list preserve the key with an empty list value.

    '''
    result = parse_endpoints({'x': []})
    assert result == {'x': []}
