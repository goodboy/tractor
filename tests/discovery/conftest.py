'''
Discovery-suite fixtures, including the `daemon` remote-registrar
subprocess used by the multi-program discovery tests.

Lives here (vs. the parent `tests/conftest.py`)
because `daemon` is a discovery-protocol primitive: it boots a child
that enters `open_root_actor()` and waits as a registrar peer for
discovery-roundtrip tests. Pytest fixtures inherit
DOWNWARD through conftest hierarchy, so anything
under `tests/discovery/` automatically picks this up.

'''
from __future__ import annotations
from pathlib import Path
import platform
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
    ready_path: Path,
    *,
    deadline: float = 10.0,
    poll_interval: float = 0.05,
    proc: subprocess.Popen|None = None,
) -> None:
    '''
    Poll until the daemon reports completed actor startup.

    Replaces the historical blind `time.sleep()` in the
    `daemon` fixture which was racy under load — see
    `ai/conc-anal/test_register_duplicate_name_daemon_connect_race_issue.md`.

    The child writes `ready_path` only after entering
    `open_root_actor()`, which guarantees all transport listeners are
    serving without requiring a raw connection probe.

    Raises `TimeoutError` on `deadline` exceeded. If
    `proc` is given, ALSO raises early if the daemon
    process exits before the deadline (catches a daemon startup crash
    that the blind sleep used to silently mask).

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
                f'before reporting ready at {ready_path!r}'
            )
        try:
            if ready_path.is_file():
                if proc is not None and proc.poll() is not None:
                    raise RuntimeError(
                        f'Daemon proc exited (rc={proc.returncode}) '
                        f'after reporting ready at {ready_path!r}'
                    )
                return
        except (
            FileNotFoundError,
            OSError,
        ) as exc:
            last_exc = exc
        time.sleep(poll_interval)
    raise TimeoutError(
        f'Daemon never reported ready at {ready_path!r} within '
        f'{deadline}s (last sentinel-state exc: {last_exc!r})'
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

    ready_path: Path = (
        Path(str(testdir.tmpdir))
        / 'daemon-ready'
    )
    ready_path.unlink(missing_ok=True)
    code: str = (
        f'from pathlib import Path\n'
        f'import tractor\n'
        f'import trio\n'
        f'\n'
        f'async def main():\n'
        f'    async with tractor.open_root_actor(\n'
        f'        registry_addrs={[reg_addr]!r},\n'
        f'        enable_transports={[tpt_proto]!r},\n'
        f'        debug_mode={debug_mode!r},\n'
        f'        loglevel={loglevel!r},\n'
        f'    ):\n'
        f'        Path({str(ready_path)!r}).touch()\n'
        f'        await trio.sleep_forever()\n'
        f'\n'
        f'trio.run(main)\n'
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

    # Poll the child's ready sentinel, published after actor startup,
    # instead of connecting to its transport socket. This replaces
    # the legacy blind `time.sleep(2.2)` which was racy under load
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
    try:
        _wait_for_daemon_ready(
            ready_path=ready_path,
            deadline=deadline,
            proc=proc,
        )

        assert not proc.returncode
        yield proc
    finally:
        if proc.poll() is None:
            sig_prog(proc, _INT_SIGNAL)

        # NOTE: these blocking reads can hang when descendants retain
        # inherited pipe descriptors. Keep teardown signaling above
        # them and avoid adding subprocesses outside the actor tree.
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
