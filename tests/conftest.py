"""
Top level of the testing suites!

"""
from __future__ import annotations
import sys
import subprocess
import os
import signal
import socket
import platform
import time
from pathlib import Path
from typing import Literal

import pytest
import tractor
from tractor._testing import (
    examples_dir as examples_dir,
    tractor_test as tractor_test,
    expect_ctxc as expect_ctxc,
)

pytest_plugins: list[str] = [
    'pytester',
    # NOTE, now loaded in `pytest-ini` section of `pyproject.toml`
    # 'tractor._testing.pytest',
]

_ci_env: bool = os.environ.get('CI', False)
_non_linux: bool = platform.system() != 'Linux'

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
        2 if _ci_env
        else 1
    )


no_windows = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="Test is unsupported on windows",
)
no_macos = pytest.mark.skipif(
    platform.system() == "Darwin",
    reason="Test is unsupported on MacOS",
)


def get_cpu_state(
    icpu: int = 0,
    setting: Literal[
        'scaling_governor',
        '*_pstate_max_freq',
        'scaling_max_freq',
        # 'scaling_cur_freq',
    ] = '*_pstate_max_freq',
) -> tuple[
    Path,
    str|int,
]|None:
    '''
    Attempt to read the (first) CPU's setting according
    to the set `setting` from under the file-sys,

    /sys/devices/system/cpu/cpu0/cpufreq/{setting}

    Useful to determine latency headroom for various perf affected
    test suites.

    '''
    try:
        # Read governor for core 0 (usually same for all)
        setting_path: Path = list(
            Path(f'/sys/devices/system/cpu/cpu{icpu}/cpufreq/')
            .glob(f'{setting}')
        )[0]  # <- XXX must be single match!
        with open(
            setting_path,
            'r',
        ) as f:
            return (
                setting_path,
                f.read().strip(),
            )
    except (FileNotFoundError, IndexError):
        return None


def cpu_scaling_factor() -> float:
    '''
    Return a latency-headroom multiplier (>= 1.0) reflecting how
    much to inflate time-limits when CPU-freq scaling is active on
    linux.

    When no scaling info is available (non-linux, missing sysfs),
    returns 1.0 (i.e. no headroom adjustment needed).

    '''
    if _non_linux:
        return 1.

    mx = get_cpu_state()
    cur = get_cpu_state(setting='scaling_max_freq')
    if mx is None or cur is None:
        return 1.

    _mx_pth, max_freq = mx
    _cur_pth, cur_freq = cur
    cpu_scaled: float = int(cur_freq) / int(max_freq)

    if cpu_scaled != 1.:
        return 1. / (
            cpu_scaled * 2  # <- bc likely "dual threaded"
        )

    return 1.


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
        default=None,
        help="logging level to set when testing",
    )


@pytest.fixture(scope='session', autouse=True)
def loglevel(
    request: pytest.FixtureRequest,
) -> str|None:
    import tractor
    orig = tractor.log._default_loglevel
    flag_level: str|None = request.config.option.loglevel

    if flag_level is not None:
        tractor.log._default_loglevel = flag_level

    log = tractor.log.get_console_log(
        level=flag_level,
        name='tractor',  # <- enable root logger
    )
    log.info(
        f'Test-harness set runtime loglevel: {flag_level!r}\n'
    )
    yield flag_level
    tractor.log._default_loglevel = orig


@pytest.fixture(scope='function')
def test_log(
    request: pytest.FixtureRequest,
    loglevel: str,
) -> tractor.log.StackLevelAdapter:
    '''
    Deliver a per test-module-fn logger instance for reporting from
    within actual test bodies/fixtures.

    For example this can be handy to report certain error cases from
    exception handlers using `test_log.exception()`.

    '''
    modname: str = request.function.__module__
    log = tractor.log.get_logger(
        name=modname,  # <- enable root logger
        # pkg_name='tests',
    )
    _log = tractor.log.get_console_log(
        level=loglevel,
        logger=log,
        name=modname,
        # pkg_name='tests',
    )
    _log.debug(
        f'In-test-logging requested\n'
        f'test_log.name: {log.name!r}\n'
        f'level: {loglevel!r}\n'

    )
    yield _log


@pytest.fixture(scope='session')
def ci_env() -> bool:
    '''
    Detect CI environment.

    '''
    return _ci_env


def sig_prog(
    proc: subprocess.Popen,
    sig: int,
    canc_timeout: float = 0.2,
    tries: int = 3,
) -> int:
    '''
    Kill the actor-process with `sig`.

    Prefer to kill with the provided signal and
    failing a `canc_timeout`, send a `SIKILL`-like
    to ensure termination.

    '''
    for i in range(tries):
        proc.send_signal(sig)
        if proc.poll() is None:
            print(
                f'WARNING, proc still alive after,\n'
                f'canc_timeout={canc_timeout!r}\n'
                f'sig={sig!r}\n'
                f'\n'
                f'{proc.args!r}\n'
            )
            time.sleep(canc_timeout)
    else:
        # TODO: why sometimes does SIGINT not work on teardown?
        # seems to happen only when trace logging enabled?
        if proc.poll() is None:
            print(
                f'XXX WARNING KILLING PROG WITH SIGINT XXX\n'
                f'canc_timeout={canc_timeout!r}\n'
                f'{proc.args!r}\n'
            )
            proc.send_signal(_KILL_SIGNAL)

    ret: int = proc.wait()
    assert ret


def _wait_for_daemon_ready(
    reg_addr: tuple,
    tpt_proto: str,
    *,
    deadline: float = 10.0,
    poll_interval: float = 0.05,
    proc: subprocess.Popen|None = None,
) -> None:
    '''
    Active-poll the daemon's bind address until it
    accepts a connection (proving it has called
    `bind() + listen()` and is ready to handle IPC).

    Replaces the historical blind `time.sleep()` in the
    `daemon` fixture which was racy under load — see
    `ai/conc-anal/test_register_duplicate_name_daemon_connect_race_issue.md`.

    Uses stdlib `socket` directly (no trio runtime
    bootstrap cost) — sufficient because
    `tractor.run_daemon()` doesn't return from
    bootstrap until the runtime is fully ready to
    accept IPC.

    Raises `TimeoutError` on `deadline` exceeded. If
    `proc` is given, ALSO raises early if the daemon
    process exits non-zero before the deadline (catches
    daemon-startup-crash that the blind sleep used to
    silently mask).

    '''
    end: float = time.monotonic() + deadline
    last_exc: Exception|None = None
    while time.monotonic() < end:
        # Daemon-died-during-startup early-exit. Without
        # this, a crashed-on-import daemon would just
        # eat the full deadline before raising opaque
        # TimeoutError.
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f'Daemon proc exited (rc={proc.returncode}) '
                f'before becoming ready to accept on '
                f'{reg_addr!r}'
            )
        try:
            if tpt_proto == 'tcp':
                # `socket.create_connection` does the
                # `socket() + connect()` dance with a
                # builtin timeout — perfect primitive
                # for a one-shot probe.
                with socket.create_connection(
                    reg_addr,
                    timeout=poll_interval,
                ):
                    return
            else:
                # UDS — `reg_addr` is a `(filedir, sockname)`
                # tuple per `tractor.ipc._uds.UDSAddress.unwrap`.
                sockpath: str = os.path.join(*reg_addr)
                sock = socket.socket(socket.AF_UNIX)
                try:
                    sock.settimeout(poll_interval)
                    sock.connect(sockpath)
                    return
                finally:
                    sock.close()
        except (
            ConnectionRefusedError,
            FileNotFoundError,
            OSError,
            socket.timeout,
        ) as exc:
            last_exc = exc
            time.sleep(poll_interval)
    raise TimeoutError(
        f'Daemon never accepted on {reg_addr!r} within '
        f'{deadline}s (last connect-attempt exc: '
        f'{last_exc!r})'
    )


# TODO: factor into @cm and move to `._testing`?
@pytest.fixture
def daemon(
    debug_mode: bool,
    loglevel: str,
    testdir: pytest.Pytester,
    reg_addr: tuple[str, int],
    tpt_proto: str,
    ci_env: bool,
    test_log: tractor.log.StackLevelAdapter,
    # set_fork_aware_capture,

) -> subprocess.Popen:
    '''
    Run a daemon root actor as a separate actor-process tree and
    "remote registrar" for discovery-protocol related tests.

    '''
    # XXX: too much logging will lock up the subproc (smh)
    if loglevel in ('trace', 'debug'):
        test_log.warning(
            f'Test harness log level is too verbose: {loglevel!r}\n'
            f'Reducing to INFO level..'
        )
        loglevel: str = 'info'

    code: str = (
        "import tractor; "
        "tractor.run_daemon([], "
        "registry_addrs={reg_addrs}, "
        "enable_transports={enable_tpts}, "
        "debug_mode={debug_mode}, "
        "loglevel={ll})"
    ).format(
        reg_addrs=str([reg_addr]),
        enable_tpts=str([tpt_proto]),
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

    # Active-poll the daemon's bind address until it's
    # ready to accept connections — replaces the legacy
    # blind `time.sleep(_PROC_SPAWN_WAIT + uds_bonus)`
    # which was racy under load (see
    # `ai/conc-anal/test_register_duplicate_name_daemon_connect_race_issue.md`).
    #
    # Per-test deadline scales with platform: macOS/CI
    # gets extra headroom; Linux dev boxes need very
    # little.
    deadline: float = (
        15.0 if (_non_linux and ci_env)
        else 10.0
    )
    _wait_for_daemon_ready(
        reg_addr=reg_addr,
        tpt_proto=tpt_proto,
        deadline=deadline,
        proc=proc,
    )

    assert not proc.returncode
    yield proc
    sig_prog(proc, _INT_SIGNAL)

    # XXX! yeah.. just be reaaal careful with this bc sometimes it
    # can lock up on the `_io.BufferedReader` and hang..
    stderr: str = proc.stderr.read().decode()
    stdout: str = proc.stdout.read().decode()
    if (
        stderr
        or
        stdout
    ):
        print(
            f'Daemon actor tree produced output:\n'
            f'{proc.args}\n'
            f'\n'
            f'stderr: {stderr!r}\n'
            f'stdout: {stdout!r}\n'
        )

    if (rc := proc.returncode) != -2:
        msg: str = (
            f'Daemon actor tree was not cancelled !?\n'
            f'proc.args: {proc.args!r}\n'
            f'proc.returncode: {rc!r}\n'
        )
        if rc < 0:
            raise RuntimeError(msg)

        test_log.error(msg)


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
