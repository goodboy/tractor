"""
``tractor`` testing!!
"""
import random
import platform

import pytest
import tractor

# export for tests
from tractor.testing import tractor_test  # noqa


pytest_plugins = ['pytester']
_arb_addr = '127.0.0.1', random.randint(1000, 9999)


no_windows = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="Test is unsupported on windows",
)


def pytest_addoption(parser):
    parser.addoption(
        "--ll", action="store", dest='loglevel',
        default=None, help="logging level to set when testing"
    )

    parser.addoption(
        "--spawn-backend", action="store", dest='spawn_backend',
        default='trio',
        help="Processing spawning backend to use for test run",
    )


def pytest_configure(config):
    backend = config.option.spawn_backend

    if backend == 'mp':
        tractor._spawn.try_set_start_method('spawn')
    elif backend == 'trio':
        tractor._spawn.try_set_start_method(backend)


@pytest.fixture(scope='session', autouse=True)
def loglevel(request):
    orig = tractor.log._default_loglevel
    level = tractor.log._default_loglevel = request.config.option.loglevel
    yield level
    tractor.log._default_loglevel = orig


@pytest.fixture(scope='session')
def arb_addr():
    return _arb_addr


def pytest_generate_tests(metafunc):
    spawn_backend = metafunc.config.option.spawn_backend
    if not spawn_backend:
        # XXX some weird windows bug with `pytest`?
        spawn_backend = 'mp'
    assert spawn_backend in ('mp', 'trio')

    if 'start_method' in metafunc.fixturenames:
        if spawn_backend == 'mp':
            from multiprocessing import get_all_start_methods
            methods = get_all_start_methods()
            if 'fork' in methods:
                # fork not available on windows, so check before
                # removing XXX: the fork method is in general
                # incompatible with trio's global scheduler state
                methods.remove('fork')
        elif spawn_backend == 'trio':
            methods = ['trio']

        metafunc.parametrize("start_method", methods, scope='module')
