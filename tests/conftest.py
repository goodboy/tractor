"""
``tractor`` testing!!
"""
import random
import platform

import pytest
import tractor
from tractor.testing import tractor_test


pytest_plugins = ['pytester']
_arb_addr = '127.0.0.1', random.randint(1000, 9999)


def pytest_addoption(parser):
    parser.addoption("--ll", action="store", dest='loglevel',
                     default=None, help="logging level to set when testing")


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
    if 'start_method' in metafunc.fixturenames:

        from multiprocessing import get_all_start_methods
        methods = get_all_start_methods()

        if platform.system() != "Windows":
            methods += ['trio_run_in_process']

        if 'fork' in methods:  # fork not available on windows, so check before removing
            # XXX: the fork method is in general incompatible with
            # trio's global scheduler state
            methods.remove('fork')
        metafunc.parametrize("start_method", methods, scope='module')
