"""
Top level of the testing suites!

"""
from __future__ import annotations
import sys
import subprocess
import os
import signal
import platform
import time

import pytest
from tractor._testing import (
    examples_dir as examples_dir,
    tractor_test as tractor_test,
    expect_ctxc as expect_ctxc,
)

pytest_plugins: list[str] = [
    'pytester',
    'tractor._testing.pytest',
]


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
    # ?TODO? should this be exposed from our `._testing.pytest`
    # plugin or should we make it more explicit with `--tl` for
    # tractor logging like we do in other client projects?
    parser.addoption(
        "--ll",
        action="store",
        dest='loglevel',
        default='ERROR', help="logging level to set when testing"
    )


@pytest.fixture(scope='session', autouse=True)
def loglevel(request):
    import tractor
    orig = tractor.log._default_loglevel
    level = tractor.log._default_loglevel = request.config.option.loglevel
    log = tractor.log.get_console_log(
        level=level,
        name='tractor',  # <- enable root logger
    )
    log.info(f'Test-harness logging level: {level}\n')
    yield level
    tractor.log._default_loglevel = orig


_ci_env: bool = os.environ.get('CI', False)


@pytest.fixture(scope='session')
def ci_env() -> bool:
    '''
    Detect CI environment.

    '''
    return _ci_env


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
    testdir: pytest.Pytester,
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
