"""
Top level of the testing suites!

"""
from __future__ import annotations
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
    _PROC_SPAWN_WAIT = (
        0.6
        if sys.version_info < (3, 7)
        else 0.4
    )


no_windows = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="Test is unsupported on windows",
)


def pytest_addoption(
    parser: pytest.Parser,
):
    parser.addoption(
        "--ll",
        action="store",
        dest='loglevel',
        default='ERROR', help="logging level to set when testing"
    )

    parser.addoption(
        "--spawn-backend",
        action="store",
        dest='spawn_backend',
        default='trio',
        help="Processing spawning backend to use for test run",
    )

    parser.addoption(
        "--tpdb",
        "--debug-mode",
        action="store_true",
        dest='tractor_debug_mode',
        # default=False,
        help=(
            'Enable a flag that can be used by tests to to set the '
            '`debug_mode: bool` for engaging the internal '
            'multi-proc debugger sys.'
        ),
    )

    # provide which IPC transport protocols opting-in test suites
    # should accumulatively run against.
    parser.addoption(
        "--tpt-proto",
        nargs='+',  # accumulate-multiple-args
        action="store",
        dest='tpt_protos',
        default=['tcp'],
        help="Transport protocol to use under the `tractor.ipc.Channel`",
    )


def pytest_configure(config):
    backend = config.option.spawn_backend
    tractor._spawn.try_set_start_method(backend)


@pytest.fixture(scope='session')
def debug_mode(request) -> bool:
    debug_mode: bool = request.config.option.tractor_debug_mode
    # if debug_mode:
    #     breakpoint()
    return debug_mode


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


@pytest.fixture(scope='session')
def tpt_protos(request) -> list[str]:

    # allow quoting on CLI
    proto_keys: list[str] = [
        proto_key.replace('"', '').replace("'", "")
        for proto_key in request.config.option.tpt_protos
    ]

    # ?TODO, eventually support multiple protos per test-sesh?
    if len(proto_keys) > 1:
        pytest.fail(
            'We only support one `--tpt-proto <key>` atm!\n'
        )

    # XXX ensure we support the protocol by name via lookup!
    for proto_key in proto_keys:
        addr_type = tractor._addr._address_types[proto_key]
        assert addr_type.proto_key == proto_key

    yield proto_keys


@pytest.fixture(scope='session')
def tpt_proto(
    tpt_protos: list[str],
) -> str:
    yield tpt_protos[0]


_ci_env: bool = os.environ.get('CI', False)


@pytest.fixture(scope='session')
def ci_env() -> bool:
    '''
    Detect CI environment.

    '''
    return _ci_env


# TODO: also move this to `._testing` for now?
# -[ ] possibly generalize and re-use for multi-tree spawning
#    along with the new stuff for multi-addrs?
#
# choose random port at import time
_rando_port: str = random.randint(1000, 9999)


@pytest.fixture(scope='session')
def reg_addr(
    tpt_proto: str,
) -> tuple[str, int|str]:

    # globally override the runtime to the per-test-session-dynamic
    # addr so that all tests never conflict with any other actor
    # tree using the default.
    from tractor import (
        _addr,
    )
    addr_type = _addr._address_types[tpt_proto]
    def_reg_addr: tuple[str, int] = _addr._default_lo_addrs[tpt_proto]

    testrun_reg_addr: tuple[str, int]
    match tpt_proto:
        case 'tcp':
            testrun_reg_addr = (
                addr_type.def_bindspace,
                _rando_port,
            )

        # NOTE, file-name uniqueness (no-collisions) will be based on
        # the runtime-directory and root (pytest-proc's) pid.
        case 'uds':
            testrun_reg_addr = addr_type.get_random().unwrap()

    assert def_reg_addr != testrun_reg_addr
    return testrun_reg_addr


def pytest_generate_tests(metafunc):
    spawn_backend: str = metafunc.config.option.spawn_backend

    if not spawn_backend:
        # XXX some weird windows bug with `pytest`?
        spawn_backend = 'trio'

    # TODO: maybe just use the literal `._spawn.SpawnMethodKey`?
    assert spawn_backend in (
        'mp_spawn',
        'mp_forkserver',
        'trio',
    )

    # NOTE: used-to-be-used-to dyanmically parametrize tests for when
    # you just passed --spawn-backend=`mp` on the cli, but now we expect
    # that cli input to be manually specified, BUT, maybe we'll do
    # something like this again in the future?
    if 'start_method' in metafunc.fixturenames:
        metafunc.parametrize(
            "start_method",
            [spawn_backend],
            scope='module',
        )

    # TODO, parametrize any `tpt_proto: str` declaring tests!
    # proto_tpts: list[str] = metafunc.config.option.proto_tpts
    # if 'tpt_proto' in metafunc.fixturenames:
    #     metafunc.parametrize(
    #         'tpt_proto',
    #         proto_tpts,  # TODO, double check this list usage!
    #         scope='module',
    #     )


def sig_prog(
    proc: subprocess.Popen,
    sig: int,
    canc_timeout: float = 0.1,
) -> int:
    "Kill the actor-process with ``sig``."
    proc.send_signal(sig)
    time.sleep(canc_timeout)
    if not proc.poll():
        # TODO: why sometimes does SIGINT not work on teardown?
        # seems to happen only when trace logging enabled?
        proc.send_signal(_KILL_SIGNAL)
    ret: int = proc.wait()
    assert ret


# TODO: factor into @cm and move to `._testing`?
@pytest.fixture
def daemon(
    debug_mode: bool,
    loglevel: str,
    testdir,
    reg_addr: tuple[str, int],
    tpt_proto: str,

) -> subprocess.Popen:
    '''
    Run a daemon root actor as a separate actor-process tree and
    "remote registrar" for discovery-protocol related tests.

    '''
    if loglevel in ('trace', 'debug'):
        # XXX: too much logging will lock up the subproc (smh)
        loglevel: str = 'info'

    code: str = (
        "import tractor; "
        "tractor.run_daemon([], "
        "registry_addrs={reg_addrs}, "
        "debug_mode={debug_mode}, "
        "loglevel={ll})"
    ).format(
        reg_addrs=str([reg_addr]),
        ll="'{}'".format(loglevel) if loglevel else None,
        debug_mode=debug_mode,
    )
    cmd: list[str] = [
        sys.executable,
        '-c', code,
    ]
    # breakpoint()
    kwargs = {}
    if platform.system() == 'Windows':
        # without this, tests hang on windows forever
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc: subprocess.Popen = testdir.popen(
        cmd,
        **kwargs,
    )

    # UDS sockets are **really** fast to bind()/listen()/connect()
    # so it's often required that we delay a bit more starting
    # the first actor-tree..
    if tpt_proto == 'uds':
        global _PROC_SPAWN_WAIT
        _PROC_SPAWN_WAIT = 0.6

    time.sleep(_PROC_SPAWN_WAIT)

    assert not proc.returncode
    yield proc
    sig_prog(proc, _INT_SIGNAL)

    # XXX! yeah.. just be reaaal careful with this bc sometimes it
    # can lock up on the `_io.BufferedReader` and hang..
    stderr: str = proc.stderr.read().decode()
    if stderr:
        print(
            f'Daemon actor tree produced STDERR:\n'
            f'{proc.args}\n'
            f'\n'
            f'{stderr}\n'
        )
    if proc.returncode != -2:
        raise RuntimeError(
            'Daemon actor tree failed !?\n'
            f'{proc.args}\n'
        )


# @pytest.fixture(autouse=True)
# def shared_last_failed(pytestconfig):
#     val = pytestconfig.cache.get("example/value", None)
#     breakpoint()
#     if val is None:
#         pytestconfig.cache.set("example/value", val)
#     return val


# TODO: a way to let test scripts (like from `examples/`)
# guarantee they won't `registry_addrs` collide!
# -[ ] maybe use some kinda standard `def main()` arg-spec that
#     we can introspect from a fixture that is called from the test
#     body?
# -[ ] test and figure out typing for below prototype! Bp
#
# @pytest.fixture
# def set_script_runtime_args(
#     reg_addr: tuple,
# ) -> Callable[[...], None]:

#     def import_n_partial_in_args_n_triorun(
#         script: Path,  # under examples?
#         **runtime_args,
#     ) -> Callable[[], Any]:  # a `partial`-ed equiv of `trio.run()`

#         # NOTE, below is taken from
#         # `.test_advanced_faults.test_ipc_channel_break_during_stream`
#         mod: ModuleType = import_path(
#             examples_dir() / 'advanced_faults'
#             / 'ipc_failure_during_stream.py',
#             root=examples_dir(),
#             consider_namespace_packages=False,
#         )
#         return partial(
#             trio.run,
#             partial(
#                 mod.main,
#                 **runtime_args,
#             )
#         )
#     return import_n_partial_in_args_n_triorun
