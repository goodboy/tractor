"""
``tractor`` testing!!
"""
import sys
import subprocess
import os
import random
import signal
import platform
import time

import pytest
import tractor
from tractor._testing import (
    examples_dir as examples_dir,
    tractor_test as tractor_test,
    expect_ctxc as expect_ctxc,
)

# TODO: include wtv plugin(s) we build in `._testing.pytest`?
pytest_plugins = ['pytester']

# Sending signal.SIGINT on subprocess fails on windows. Use CTRL_* alternatives
if platform.system() == 'Windows':
    _KILL_SIGNAL = signal.CTRL_BREAK_EVENT
    _INT_SIGNAL = signal.CTRL_C_EVENT
    _INT_RETURN_CODE = 3221225786
    _PROC_SPAWN_WAIT = 2
else:
    _KILL_SIGNAL = signal.SIGKILL
    _INT_SIGNAL = signal.SIGINT
    _INT_RETURN_CODE = 1 if sys.version_info < (3, 8) else -signal.SIGINT.value
    _PROC_SPAWN_WAIT = 0.6 if sys.version_info < (3, 7) else 0.4


no_windows = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="Test is unsupported on windows",
)


def pytest_addoption(parser):
    parser.addoption(
        "--ll", action="store", dest='loglevel',
        default='ERROR', help="logging level to set when testing"
    )

    parser.addoption(
        "--spawn-backend", action="store", dest='spawn_backend',
        default='trio',
        help="Processing spawning backend to use for test run",
    )


def pytest_configure(config):
    backend = config.option.spawn_backend
    tractor._spawn.try_set_start_method(backend)


@pytest.fixture(scope='session', autouse=True)
def loglevel(request):
    orig = tractor.log._default_loglevel
    level = tractor.log._default_loglevel = request.config.option.loglevel
    tractor.log.get_console_log(level)
    yield level
    tractor.log._default_loglevel = orig


@pytest.fixture(scope='session')
def spawn_backend(request) -> str:
    return request.config.option.spawn_backend


_ci_env: bool = os.environ.get('CI', False)


@pytest.fixture(scope='session')
def ci_env() -> bool:
    '''
    Detect CI envoirment.

    '''
    return _ci_env


# TODO: also move this to `._testing` for now?
# -[ ] possibly generalize and re-use for multi-tree spawning
#    along with the new stuff for multi-addrs in distribute_dis
#    branch?
#
# choose randomly at import time
_reg_addr: tuple[str, int] = (
    '127.0.0.1',
    random.randint(1000, 9999),
)
_arb_addr = _reg_addr


@pytest.fixture(scope='session')
def arb_addr():
    return _arb_addr


def pytest_generate_tests(metafunc):
    spawn_backend = metafunc.config.option.spawn_backend

    if not spawn_backend:
        # XXX some weird windows bug with `pytest`?
        spawn_backend = 'trio'

    # TODO: maybe just use the literal `._spawn.SpawnMethodKey`?
    assert spawn_backend in (
        'mp_spawn',
        'mp_forkserver',
        'trio',
    )

    # NOTE: used to be used to dyanmically parametrize tests for when
    # you just passed --spawn-backend=`mp` on the cli, but now we expect
    # that cli input to be manually specified, BUT, maybe we'll do
    # something like this again in the future?
    if 'start_method' in metafunc.fixturenames:
        metafunc.parametrize("start_method", [spawn_backend], scope='module')


def sig_prog(proc, sig):
    "Kill the actor-process with ``sig``."
    proc.send_signal(sig)
    time.sleep(0.1)
    if not proc.poll():
        # TODO: why sometimes does SIGINT not work on teardown?
        # seems to happen only when trace logging enabled?
        proc.send_signal(_KILL_SIGNAL)
    ret = proc.wait()
    assert ret


# TODO: factor into @cm and move to `._testing`?
@pytest.fixture
def daemon(
    loglevel: str,
    testdir,
    arb_addr: tuple[str, int],
):
    '''
    Run a daemon actor as a "remote arbiter".

    '''
    if loglevel in ('trace', 'debug'):
        # too much logging will lock up the subproc (smh)
        loglevel = 'info'

    cmdargs = [
        sys.executable, '-c',
        "import tractor; tractor.run_daemon([], registry_addr={}, loglevel={})"
        .format(
            arb_addr,
            "'{}'".format(loglevel) if loglevel else None)
    ]
    kwargs = dict()
    if platform.system() == 'Windows':
        # without this, tests hang on windows forever
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = testdir.popen(
        cmdargs,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    assert not proc.returncode
    time.sleep(_PROC_SPAWN_WAIT)
    yield proc
    sig_prog(proc, _INT_SIGNAL)
