"""
``tractor`` testing!!
"""
import pytest
import tractor


def pytest_addoption(parser):
    parser.addoption("--ll", action="store", dest='loglevel',
                     default=None, help="logging level to set when testing")


@pytest.fixture(scope='session', autouse=True)
def loglevel(request):
    orig = tractor._default_loglevel
    level = tractor._default_loglevel = request.config.option.loglevel
    yield level
    tractor._default_loglevel = orig
