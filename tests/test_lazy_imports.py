'''
Regression tests for the cold package import surface.

'''
import json
import subprocess
import sys
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


def run_cold_import(code: str) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            '-c',
            code,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_lazy_to_asyncio_package_api():
    '''
    Keep the public lazy submodule discoverable without eagerly
    importing it.

    Before the lazy conversion, package import side effects exposed
    `to_asyncio` to `dir()` and wildcard imports. Exercise those APIs
    in cold interpreters so this test proves normal `import tractor`
    leaves `asyncio` unloaded, while discovery and wildcard access
    still advertise and resolve the public submodule.

    '''
    cold = run_cold_import(
        'import json, sys, tractor; '
        'print(json.dumps({'
        '"advertised": "to_asyncio" in dir(tractor), '
        '"asyncio_loaded": "asyncio" in sys.modules}))'
    )
    assert cold == {
        'advertised': True,
        'asyncio_loaded': False,
    }

    wildcard = run_cold_import(
        'import json; '
        'from tractor import *; '
        'print(json.dumps({'
        '"module": to_asyncio.__name__}))'
    )
    assert wildcard == {
        'module': 'tractor.to_asyncio',
    }


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
