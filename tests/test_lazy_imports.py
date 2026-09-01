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

import tractor
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
    leaves `asyncio` unloaded, while introspection and wildcard
    access still advertise and resolve the public submodule.

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
        'multibase',
        'pyroute2',
        'tractor.discovery._multiaddr',
        'tractor.net',
        'tractor.net._bindspace',
        'tractor.net._tunnel',
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


def test_lazy_net_package_api():
    '''
    Keep the public network package cold until symbol access.

    The old discovery re-exports imported bindspace, tunnel,
    multiaddr and optional dependencies while initializing a package.
    Import `tractor.net` in a clean interpreter, inspect its public
    surface, and prove no implementation or optional dependency was
    loaded. Then resolve one symbol from each backing module and
    prove the facade caches each value while preserving boundaries.

    '''
    modules: tuple[str, ...] = (
        'tractor.net._bindspace',
        'tractor.net._tunnel',
        'tractor.discovery._multiaddr',
        'multiaddr',
        'multibase',
        'pyroute2',
    )
    cold: dict[str, object] = run_cold_import(
        'import json, sys; import tractor.net as net; '
        f'names = {modules!r}; '
        'print(json.dumps({'
        '"public": all(name in dir(net) for name in net.__all__), '
        '"loaded": [name for name in names if name in sys.modules]'
        '}))'
    )
    assert cold == {
        'public': True,
        'loaded': [],
    }

    resolved: dict[str, object] = run_cold_import(
        'import json, sys; import tractor.net as net; '
        'bindspace = net.BindspaceSpec; '
        'bindspace_cached = net.BindspaceSpec is bindspace; '
        'maddr = net.mk_maddr; '
        'maddr_cached = net.mk_maddr is maddr; '
        'tunnel = net.WGTunnelSpec; '
        'tunnel_cached = net.WGTunnelSpec is tunnel; '
        'print(json.dumps({'
        '"bindspace_cached": bindspace_cached, '
        '"maddr_cached": maddr_cached, '
        '"tunnel_cached": tunnel_cached, '
        '"bindspace_module": bindspace.__module__, '
        '"maddr_module": maddr.__module__, '
        '"tunnel_module": tunnel.__module__, '
        '"multiaddr_loaded": "multiaddr" in sys.modules, '
        '"pyroute2_loaded": "pyroute2" in sys.modules'
        '}))'
    )
    assert resolved == {
        'bindspace_cached': True,
        'maddr_cached': True,
        'tunnel_cached': True,
        'bindspace_module': 'tractor.net._bindspace',
        'maddr_module': 'tractor.discovery._multiaddr',
        'tunnel_module': 'tractor.net._tunnel',
        'multiaddr_loaded': False,
        'pyroute2_loaded': False,
    }


def test_net_root_export_and_old_discovery_surface():
    '''
    Publish networking only from its approved namespace.

    Before extraction, unshipped network names and implementation
    modules lived under `tractor.discovery`. Exercise root attribute
    and wildcard access in clean interpreters, proving `tractor.net`
    is discoverable and cached without loading implementations. Also
    prove the old exports are absent and their modules no longer
    resolve, preventing accidental compatibility aliases.

    '''
    root: dict[str, object] = run_cold_import(
        'import json, sys, tractor; '
        'advertised = "net" in dir(tractor); '
        'net = tractor.net; '
        'print(json.dumps({'
        '"advertised": advertised, '
        '"cached": tractor.net is net, '
        '"module": net.__name__, '
        '"bindspace_loaded": '
        '"tractor.net._bindspace" in sys.modules, '
        '"tunnel_loaded": "tractor.net._tunnel" in sys.modules'
        '}))'
    )
    assert root == {
        'advertised': True,
        'cached': True,
        'module': 'tractor.net',
        'bindspace_loaded': False,
        'tunnel_loaded': False,
    }

    old: dict[str, object] = run_cold_import(
        'import importlib.util, json; '
        'import tractor.discovery as discovery; '
        'old_names = ("Bindspace", "TunnelledAddress", '
        '"mk_maddr", "parse_maddr", "parse_endpoints"); '
        'old_modules = ("tractor.discovery._bindspace", '
        '"tractor.discovery._tunnel"); '
        'print(json.dumps({'
        '"exports": [name for name in old_names '
        'if hasattr(discovery, name)], '
        '"modules": [name for name in old_modules '
        'if importlib.util.find_spec(name) is not None]'
        '}))'
    )
    assert old == {
        'exports': [],
        'modules': [],
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
    assert get_type_hints(tractor.open_root_actor)[
        'bindspace'
    ] == Any|None
    assert _addr.__annotations__['_address_types'].startswith('dict')
