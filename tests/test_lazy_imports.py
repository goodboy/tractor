'''
Regression tests for the cold package import surface.

'''
from typing import (
    Any,
    get_type_hints,
)

from tractor.discovery import (
    _addr,
    _multiaddr,
)
from tractor.ipc import (
    _tcp,
    _uds,
)


def test_lazy_annotation_names_resolve():
    '''
    Resolve annotations without importing optional dependencies.

    Moving annotation-only third-party names under `TYPE_CHECKING`
    left their runtime globals undefined, causing
    `typing.get_type_hints()` to raise `NameError`. Resolve every
    affected API and prove the lazy aliases retain import-free runtime
    introspection.

    '''
    assert get_type_hints(_multiaddr.mk_maddr)['return'] is Any
    assert get_type_hints(_tcp.MsgpackTCPStream.maddr.fget)[
        'return'
    ] is Any
    assert get_type_hints(_uds.MsgpackUDSStream.maddr.fget)[
        'return'
    ] == Any|str
    assert get_type_hints(_addr.Address.get_random)[
        'current_actor'
    ] is Any
    assert _addr.__annotations__['_address_types'].startswith('dict')
