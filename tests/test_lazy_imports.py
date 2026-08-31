'''
Regression tests for the cold package import surface.

'''
import json
import os
from statistics import median
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
    _tipc,
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


def test_cold_import_budget():
    '''
    Keep cold package import below the pre-optimization regression.

    The original `inspect.stack()` caller lookup made a fresh
    `import tractor` take about 0.42s and dominate actor startup.
    Run seven independent interpreters and gate their median at a
    deliberately broad 0.35s: over twice the measured ~0.145s
    baseline, but low enough to catch restoration of that hot path.

    Taking the median absorbs process-start and shared-runner noise.
    The child measures only its import, rather than parent-side
    process creation. `TRACTOR_IMPORT_BUDGET_S` provides an explicit,
    reviewable override for platforms that establish a different
    baseline instead of silently weakening the project default.

    Each child also reports the modules whose eager loading this PR
    intentionally removes, proving a timing pass cannot hide a
    dependency-import regression.

    '''
    budget_s = float(
        os.environ.get(
            'TRACTOR_IMPORT_BUDGET_S',
            '0.35',
        )
    )
    optional_mods = (
        'asyncio',
        'bidict',
        'colorlog',
        'multiaddr',
        'wrapt',
    )
    code = (
        'import json, sys, time; '
        'started = time.perf_counter(); '
        'import tractor; '
        'elapsed = time.perf_counter() - started; '
        f'optional = {optional_mods!r}; '
        'print(json.dumps({'
        '"elapsed": elapsed, '
        '"loaded": [name for name in optional '
        'if name in sys.modules]}))'
    )
    samples = [
        run_cold_import(code)
        for _ in range(7)
    ]
    elapsed = [
        float(sample['elapsed'])
        for sample in samples
    ]
    loaded = {
        name
        for sample in samples
        for name in sample['loaded']
    }

    assert not loaded
    assert median(elapsed) < budget_s, (
        f'cold import median exceeded {budget_s:.3f}s budget: '
        f'{elapsed!r}'
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
    assert get_type_hints(_multiaddr.mk_maddr)['return'] == Any|str
    assert get_type_hints(_tcp.MsgpackTCPStream.maddr.fget)[
        'return'
    ] is Any
    assert get_type_hints(_uds.MsgpackUDSStream.maddr.fget)[
        'return'
    ] == Any|str
    assert get_type_hints(_tipc.MsgpackTIPCStream.maddr.fget)[
        'return'
    ] == Any|str
    assert get_type_hints(_addr.Address.get_random)[
        'current_actor'
    ] is Any
    assert _addr.__annotations__['_address_types'].startswith('dict')
