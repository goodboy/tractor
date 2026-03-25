'''
Multiaddr construction and round-trip tests for
`tractor.discovery._multiaddr.mk_maddr()`.

'''
from pathlib import Path
from types import SimpleNamespace

import pytest
from multiaddr import Multiaddr

from tractor.ipc._tcp import TCPAddress
from tractor.ipc._uds import UDSAddress
from tractor.discovery._multiaddr import (
    mk_maddr,
    _tpt_proto_to_maddr,
)


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
    # NOTE, use a relative `filedir` since the multiaddr
    # parser rejects the double-slash from absolute paths
    # (i.e. `/unix//tmp/..` -> "empty protocol path").
    filedir = 'tractor_test'
    filename = 'test_sock.sock'
    addr = UDSAddress(
        filedir=filedir,
        filename=filename,
    )
    result: Multiaddr = mk_maddr(addr)

    assert isinstance(result, Multiaddr)

    result_str: str = str(result)
    assert result_str.startswith('/unix/')

    sockpath: str = str(Path(filedir) / filename)
    # NOTE, the multiaddr lib prepends a `/` to the
    # unix protocol value when parsing back out.
    unix_val: str = result.value_for_protocol('unix')
    assert unix_val.endswith(sockpath)


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
                filedir='tractor_rt',
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
