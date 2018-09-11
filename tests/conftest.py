"""
``tractor`` testing!!
"""
import random
from functools import partial, wraps

import pytest
import tractor


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


def tractor_test(fn):
    """
    Use:

    @tractor_test
    async def test_whatever():
        await ...
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # __tracebackhide__ = True
        return tractor.run(
            partial(fn, *args, **kwargs), arbiter_addr=_arb_addr)

    return wrapper
