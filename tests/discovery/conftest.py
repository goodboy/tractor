'''
Discovery-suite fixtures, including the `daemon`
remote-registrar subprocess used by the multi-program
discovery tests.

Lives here (vs. the parent `tests/conftest.py`)
because `daemon` is a discovery-protocol primitive —
boots a separate `tractor.run_daemon()` process whose
sole purpose is to serve as a registrar peer for
discovery-roundtrip tests. Pytest fixtures inherit
DOWNWARD through conftest hierarchy, so anything
under `tests/discovery/` automatically picks this up.

'''
from __future__ import annotations
import os
import platform
import socket
import subprocess
import sys
import time

import pytest
import tractor

from ..conftest import (
    sig_prog,
    _INT_SIGNAL,
    _non_linux,
)


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

) -> subprocess.Popen:
    '''
    Run a daemon root actor as a separate actor-process
    tree and "remote registrar" for discovery-protocol
    related tests.

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
    # blind `time.sleep(2.2)` which was racy under load
    # (see
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

    # XXX! yeah.. just be reaaal careful with this bc
    # sometimes it can lock up on the `_io.BufferedReader`
    # and hang..
    #
    # NB, drain happens at TEARDOWN (post-yield), so the
    # test body has its chance to read `proc.stderr`
    # FIRST. Reading here AFTER would silently swallow
    # the daemon's stderr output and break tests that
    # assert on it (e.g. `test_abort_on_sigint`).
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
