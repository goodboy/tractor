'''
Integration exercises for the `tractor.spawn._main_thread_forkserver`
submodule at three tiers:

1. the low-level primitives
   (`fork_from_worker_thread()` from `_main_thread_forkserver`
   + `run_subint_in_worker_thread()` from
   `_subint_forkserver`) driven from inside a real
   `trio.run()` in the parent process,

2. the full `main_thread_forkserver_proc` spawn backend wired
   through tractor's normal actor-nursery + portal-RPC
   machinery — i.e. `open_root_actor` + `open_nursery` +
   `run_in_actor` against a subactor spawned via fork from a
   main-interp worker thread.

Background
----------
`ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md`
establishes that `os.fork()` from a non-main sub-interpreter
aborts the child at the CPython level. The sibling
`subint_fork_from_main_thread_smoketest.py` proves the escape
hatch: fork from a main-interp *worker thread* (one that has
never entered a subint) works, and the forked child can then
host its own `trio.run()` inside a fresh subint.

Those smoke-test scenarios are standalone — no trio runtime
in the *parent*. Tiers (1)+(2) here cover the primitives
driven from inside `trio.run()` in the parent, and tier (3)
(the `*_spawn_basic` test) drives the registered
`main_thread_forkserver` spawn backend end-to-end against
the tractor runtime.

Gating
------
- py3.14+ (via `concurrent.interpreters` presence)
- no `--spawn-backend` restriction — the backend-level test
  flips `tractor.spawn._spawn._spawn_method` programmatically
  (via `try_set_start_method('main_thread_forkserver')`) and
  restores it on teardown, so these tests are independent of
  the session-level CLI backend choice.

'''
from __future__ import annotations
from functools import partial
import os
from pathlib import Path
import platform
import select
import signal
import subprocess
import sys
import time

import pytest
import trio

import tractor
from tractor.devx import dump_on_hang


# Gate: subint forkserver primitives require py3.14+. Check
# the public stdlib wrapper's presence (added in 3.14) rather
# than `_interpreters` directly — see
# `tractor.spawn._subint` for why.
pytest.importorskip('concurrent.interpreters')

from tractor.spawn._main_thread_forkserver import (  # noqa: E402
    fork_from_worker_thread,
    wait_child,
)
from tractor.spawn._subint_forkserver import (  # noqa: E402
    run_subint_in_worker_thread,
)
from tractor.spawn import _spawn as _spawn_mod  # noqa: E402
from tractor.spawn._spawn import try_set_start_method  # noqa: E402


# ----------------------------------------------------------------
# child-side callables (passed via `child_target=` across fork)
# ----------------------------------------------------------------


_CHILD_TRIO_BOOTSTRAP: str = (
    'import trio\n'
    'async def _main():\n'
    '    await trio.sleep(0.05)\n'
    '    return 42\n'
    'result = trio.run(_main)\n'
    'assert result == 42, f"trio.run returned {result}"\n'
)


def _child_trio_in_subint() -> int:
    '''
    `child_target` for the trio-in-child scenario: drive a
    trivial `trio.run()` inside a fresh legacy-config subint
    on a worker thread.

    Returns an exit code suitable for `os._exit()`:
    - 0: subint-hosted `trio.run()` succeeded
    - 3: driver thread hang (timeout inside `run_subint_in_worker_thread`)
    - 4: subint bootstrap raised some other exception

    '''
    try:
        run_subint_in_worker_thread(
            _CHILD_TRIO_BOOTSTRAP,
            thread_name='child-subint-trio-thread',
        )
    except RuntimeError:
        # timeout / thread-never-returned
        return 3
    except BaseException:
        return 4
    return 0


# ----------------------------------------------------------------
# parent-side harnesses (run inside `trio.run()`)
# ----------------------------------------------------------------


async def run_fork_in_non_trio_thread(
    deadline: float,
    *,
    child_target=None,
) -> int:
    '''
    From inside a parent `trio.run()`, off-load the
    forkserver primitive to a main-interp worker thread via
    `trio.to_thread.run_sync()` and return the forked child's
    pid.

    Then `wait_child()` on that pid (also off-loaded so we
    don't block trio's event loop on `waitpid()`) and assert
    the child exited cleanly.

    '''
    with trio.fail_after(deadline):
        # NOTE: `fork_from_worker_thread` internally spawns its
        # own dedicated `threading.Thread` (not from trio's
        # cache) and joins it before returning — so we can
        # safely off-load via `to_thread.run_sync` without
        # worrying about the trio-thread-cache recycling the
        # runner. Pass `abandon_on_cancel=False` for the
        # same "bounded + clean" rationale we use in
        # `_subint.subint_proc`.
        pid: int = await trio.to_thread.run_sync(
            partial(
                fork_from_worker_thread,
                child_target,
                thread_name='test-subint-forkserver',
            ),
            abandon_on_cancel=False,
        )
        assert pid > 0

        ok, status_str = await trio.to_thread.run_sync(
            partial(
                wait_child,
                pid,
                expect_exit_ok=True,
            ),
            abandon_on_cancel=False,
        )
        assert ok, (
            f'forked child did not exit cleanly: '
            f'{status_str}'
        )
        return pid


# ----------------------------------------------------------------
# tests
# ----------------------------------------------------------------


# Bounded wall-clock via `pytest-timeout` (`method='thread'`)
# for the usual GIL-hostage safety reason documented in the
# sibling `test_subint_cancellation.py` / the class-A
# `subint_sigint_starvation_issue.md`. Each test also has an
# inner `trio.fail_after()` so assertion failures fire fast
# under normal conditions.
# @pytest.mark.timeout(30, method='thread')
def test_fork_from_worker_thread_via_trio(
) -> None:
    '''
    Baseline: inside `trio.run()`, call
    `fork_from_worker_thread()` via `trio.to_thread.run_sync()`,
    get a child pid back, reap the child cleanly.

    No trio-in-child. If this regresses we know the parent-
    side trio↔worker-thread plumbing is broken independent
    of any child-side subint machinery.

    '''
    deadline: float = 10.0
    with dump_on_hang(
        seconds=deadline,
        path='/tmp/main_thread_forkserver_baseline.dump',
    ):
        pid: int = trio.run(
            partial(run_fork_in_non_trio_thread, deadline),
        )
    # parent-side sanity — we got a real pid back.
    assert isinstance(pid, int) and pid > 0
    # by now the child has been waited on; it shouldn't be
    # reap-able again.
    with pytest.raises((ChildProcessError, OSError)):
        os.waitpid(pid, os.WNOHANG)


@pytest.mark.timeout(30, method='thread')
def test_fork_and_run_trio_in_child() -> None:
    '''
    End-to-end: inside the parent's `trio.run()`, off-load
    `fork_from_worker_thread()` to a worker thread, have the
    forked child then create a fresh subint and run
    `trio.run()` inside it on yet another worker thread.

    This is the full "forkserver + trio-in-subint-in-child"
    pattern the proposed `main_thread_forkserver` spawn backend
    would rest on.

    '''
    deadline: float = 15.0
    with dump_on_hang(
        seconds=deadline,
        path='/tmp/main_thread_forkserver_trio_in_child.dump',
    ):
        pid: int = trio.run(
            partial(
                run_fork_in_non_trio_thread,
                deadline,
                child_target=_child_trio_in_subint,
            ),
        )
    assert isinstance(pid, int) and pid > 0


# ----------------------------------------------------------------
# tier-3 backend test: drive the registered `main_thread_forkserver`
# spawn backend end-to-end through tractor's actor-nursery +
# portal-RPC machinery.
# ----------------------------------------------------------------


async def _trivial_rpc() -> str:
    '''
    Minimal subactor-side RPC body: just return a sentinel
    string the parent can assert on.

    '''
    return 'hello from subint-forkserver child'


async def _happy_path_forkserver(
    reg_addr: tuple[str, int | str],
    deadline: float,
) -> None:
    '''
    Parent-side harness: stand up a root actor, open an actor
    nursery, spawn one subactor via the currently-selected
    spawn backend (which this test will have flipped to
    `main_thread_forkserver`), run a trivial RPC through its
    portal, assert the round-trip result.

    '''
    with trio.fail_after(deadline):
        async with (
            tractor.open_root_actor(
                registry_addrs=[reg_addr],
            ),
            tractor.open_nursery() as an,
        ):
            portal: tractor.Portal = await an.run_in_actor(
                _trivial_rpc,
                name='subint-forkserver-child',
            )
            result: str = await portal.wait_for_result()
            assert result == 'hello from subint-forkserver child'


@pytest.fixture
def forkserver_spawn_method():
    '''
    Flip `tractor.spawn._spawn._spawn_method` to
    `'main_thread_forkserver'` for the duration of a test,
    then restore whatever was in place before (usually the
    session-level CLI choice, typically `'trio'`).

    Without this, other tests in the same session would
    observe the global flip and start spawning via fork —
    which is almost certainly NOT what their assertions were
    written against.

    '''
    prev_method: str = _spawn_mod._spawn_method
    prev_ctx = _spawn_mod._ctx
    try_set_start_method('main_thread_forkserver')
    try:
        yield
    finally:
        _spawn_mod._spawn_method = prev_method
        _spawn_mod._ctx = prev_ctx


@pytest.mark.timeout(60, method='thread')
def test_main_thread_forkserver_spawn_basic(
    reg_addr: tuple[str, int | str],
    forkserver_spawn_method,
) -> None:
    '''
    Happy-path: spawn ONE subactor via the
    `main_thread_forkserver` backend (parent-side fork from a
    main-interp worker thread), do a trivial portal-RPC
    round-trip, tear the nursery down cleanly.

    If this passes, the "forkserver + tractor runtime" arch
    is proven end-to-end: the registered
    `main_thread_forkserver_proc` spawn target successfully
    forks a child, the child runs `_actor_child_main()` +
    completes IPC handshake + serves an RPC, and the parent
    reaps via `_ForkedProc.wait()` without regressing any of
    the normal nursery teardown invariants.

    '''
    deadline: float = 20.0
    with dump_on_hang(
        seconds=deadline,
        path='/tmp/main_thread_forkserver_spawn_basic.dump',
    ):
        trio.run(
            partial(
                _happy_path_forkserver,
                reg_addr,
                deadline,
            ),
        )


# ----------------------------------------------------------------
# tier-4 DRAFT: orphaned-subactor SIGINT survivability
#
# Motivating question: with `main_thread_forkserver`, the child's
# `trio.run()` lives on the fork-inherited worker thread which
# is NOT `threading.main_thread()` — so trio cannot install its
# `signal.set_wakeup_fd`-based SIGINT handler. If the parent
# goes away via `SIGKILL` (no IPC `Portal.cancel_actor()`
# possible), does SIGINT on the orphan child cleanly tear it
# down via CPython's default `KeyboardInterrupt` delivery, or
# does it hang?
#
# Working hypothesis (unverified pre-this-test): post-fork the
# child is effectively single-threaded (only the fork-worker
# tstate survived), so SIGINT → default handler → raises
# `KeyboardInterrupt` on the only thread — which happens to be
# the one driving trio's event loop — so trio observes it at
# the next checkpoint. If so, we're "fine" on this backend
# despite the missing trio SIGINT handler.
#
# Cross-backend generalization (decide after this passes):
# - applicable to any backend whose subactors are separate OS
#   processes: `trio`, `mp_spawn`, `mp_forkserver`,
#   `main_thread_forkserver`.
# - NOT applicable to plain `subint` (subactors are in-process
#   subinterpreters, no orphan child process to SIGINT).
# - move path: lift the harness script into
#   `tests/_orphan_harness.py`, parametrize on the session's
#   `_spawn_method`, add `skipif _spawn_method == 'subint'`.
# ----------------------------------------------------------------


_ORPHAN_HARNESS_SCRIPT: str = '''
import os
import sys
import trio
import tractor
from tractor.spawn._spawn import try_set_start_method

async def _sleep_forever() -> None:
    print(f"CHILD_PID={os.getpid()}", flush=True)
    await trio.sleep_forever()

async def _main(reg_addr):
    async with (
        tractor.open_root_actor(registry_addrs=[reg_addr]),
        tractor.open_nursery() as an,
    ):
        portal = await an.run_in_actor(
            _sleep_forever,
            name="orphan-test-child",
        )
        print(f"PARENT_READY={os.getpid()}", flush=True)
        await trio.sleep_forever()

if __name__ == "__main__":
    backend = sys.argv[1]
    host = sys.argv[2]
    port = int(sys.argv[3])
    try_set_start_method(backend)
    trio.run(_main, (host, port))
'''


def _read_marker(
    proc: subprocess.Popen,
    marker: str,
    timeout: float,
    _buf: dict,
) -> str:
    '''
    Block until `<marker>=<value>\\n` appears on `proc.stdout`
    and return `<value>`. Uses a per-proc byte buffer (`_buf`)
    to carry partial lines across calls.

    '''
    deadline: float = time.monotonic() + timeout
    remainder: bytes = _buf.get('remainder', b'')
    prefix: bytes = f'{marker}='.encode()
    while time.monotonic() < deadline:
        # drain any complete lines already buffered
        while b'\n' in remainder:
            line, remainder = remainder.split(b'\n', 1)
            if line.startswith(prefix):
                _buf['remainder'] = remainder
                return line[len(prefix):].decode().strip()
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not ready:
            continue
        chunk: bytes = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            break
        remainder += chunk
    _buf['remainder'] = remainder
    raise TimeoutError(
        f'Never observed marker {marker!r} on harness stdout '
        f'within {timeout}s'
    )


def _process_alive(pid: int) -> bool:
    '''Liveness probe for a pid we do NOT parent (post-orphan).'''
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# Known-gap test — `main_thread_forkserver` orphan-SIGINT
# handling. See
# `ai/conc-anal/subint_forkserver_orphan_sigint_hang_issue.md`.
# `strict=True` so if a future fix closes the gap the
# XPASS surfaces as a FAIL and forces us to drop the
# mark intentionally.
@pytest.mark.xfail(
    strict=True,
    reason=(
        'Orphan subactor SIGINT delivery: trio event loop '
        'on non-main thread post-fork doesn\'t see the '
        'external SIGINT → KBI path. See tracker doc.\n'
        'ai/conc-anal/subint_forkserver_orphan_sigint_hang_issue.md'
    ),
)
@pytest.mark.timeout(
    30,
    method='thread',
)
def test_orphaned_subactor_sigint_cleanup_DRAFT(
    reg_addr: tuple[str, int | str],
    tmp_path: Path,
) -> None:
    '''
    DRAFT — orphaned-subactor SIGINT survivability under the
    `main_thread_forkserver` backend.

    Sequence:
      1. Spawn a harness subprocess that brings up a root
         actor + one `sleep_forever` subactor via
         `main_thread_forkserver`.
      2. Read the harness's stdout for `PARENT_READY=<pid>`
         and `CHILD_PID=<pid>` markers (confirms the
         parent→child IPC handshake completed).
      3. `SIGKILL` the parent (no IPC cancel possible — the
         whole point of this test).
      4. `SIGINT` the orphan child.
      5. Poll `os.kill(child_pid, 0)` for up to 10s — assert
         the child exits.

    Empirical result (2026-04, py3.14): currently **FAILS** —
    SIGINT on the orphan child doesn't unwind the trio loop,
    despite trio's `KIManager` handler being correctly
    installed in the subactor (the post-fork thread IS
    `threading.main_thread()` on py3.14). `faulthandler` dump
    shows the subactor wedged in `trio/_core/_io_epoll.py::
    get_events` — the signal's supposed wakeup of the event
    loop isn't firing. Full analysis + diagnostic evidence
    in `ai/conc-anal/
    subint_forkserver_orphan_sigint_hang_issue.md`.

    The runtime's *intentional* "KBI-as-OS-cancel" path at
    `tractor/spawn/_entry.py::_trio_main:164` is therefore
    unreachable under this backend+config. Closing the gap is
    aligned with existing design intent (make the already-
    designed behavior actually fire), not a new feature.
    Marked `xfail(strict=True)` so the
    mark flips to XPASS→fail once the gap is closed and we'll
    know to drop the mark.

    '''
    if platform.system() != 'Linux':
        pytest.skip(
            'orphan-reparenting semantics only exercised on Linux'
        )

    script_path = tmp_path / '_orphan_harness.py'
    script_path.write_text(_ORPHAN_HARNESS_SCRIPT)

    # Offset the port so we don't race the session reg_addr with
    # any concurrently-running backend test's listener.
    host: str = reg_addr[0]
    port: int = int(reg_addr[1]) + 17

    proc: subprocess.Popen = subprocess.Popen(
        [
            sys.executable,
            str(script_path),
            'main_thread_forkserver',
            host,
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    parent_pid: int | None = None
    child_pid: int | None = None
    buf: dict = {}
    try:
        child_pid = int(_read_marker(proc, 'CHILD_PID', 15.0, buf))
        parent_pid = int(_read_marker(proc, 'PARENT_READY', 15.0, buf))

        # sanity: both alive before we start killing stuff
        assert _process_alive(parent_pid), (
            f'harness parent pid={parent_pid} gone before '
            f'SIGKILL — test premise broken'
        )
        assert _process_alive(child_pid), (
            f'orphan-candidate child pid={child_pid} gone '
            f'before test started'
        )

        # step 3: kill parent — no IPC cancel arrives at child.
        # `proc.wait()` reaps the zombie so it truly disappears
        # from the process table (otherwise `os.kill(pid, 0)`
        # keeps reporting it as alive).
        os.kill(parent_pid, signal.SIGKILL)
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f'harness parent pid={parent_pid} did not die '
                f'after SIGKILL — test premise broken'
            )
        assert _process_alive(child_pid), (
            f'child pid={child_pid} died along with parent — '
            f'did the parent reap it before SIGKILL took? '
            f'test premise requires an orphan.'
        )

        # step 4+5: SIGINT the orphan, poll for exit.
        os.kill(child_pid, signal.SIGINT)
        timeout: float = 6.0
        cleanup_deadline: float = time.monotonic() + timeout
        while time.monotonic() < cleanup_deadline:
            if not _process_alive(child_pid):
                return  # <- success path
            time.sleep(0.1)

        pytest.fail(
            f'Orphan subactor (pid={child_pid}) did NOT exit '
            f'within 10s of SIGINT under `main_thread_forkserver` '
            f'→ trio on non-main thread did not observe the '
            f'default CPython KeyboardInterrupt; backend needs '
            f'explicit SIGINT plumbing.'
        )
    finally:
        # best-effort cleanup to avoid leaking orphans across
        # the test session regardless of outcome.
        for pid in (parent_pid, child_pid):
            if pid is None:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


# ----------------------------------------------------------------
# regression guard: variant-2 (`subint_forkserver`) placeholder
# MUST raise `NotImplementedError` today — guards against future
# commits accidentally re-aliasing the key to the variant-1
# coroutine (which was a transient state during the rename).
# ----------------------------------------------------------------
def test_subint_forkserver_key_errors_cleanly() -> None:
    '''
    `--spawn-backend=subint_forkserver` is reserved for the
    eventual variant-2 (subint-isolated child runtime)
    backend, gated on jcrist/msgspec#1026 unblocking PEP 684
    isolated-mode subints upstream.

    Until that lands, the dispatch entry MUST raise
    `NotImplementedError` immediately rather than silently
    aliasing to `main_thread_forkserver_proc`. Verify the
    error message also surfaces both the working-backend
    pointer and the upstream-blocker ref so an operator
    arriving at the error has somewhere to go.

    '''
    import asyncio
    from tractor.spawn._spawn import _methods

    proc = _methods['subint_forkserver']
    with pytest.raises(NotImplementedError) as ei:
        # signature args match `main_thread_forkserver_proc`'s
        # — the stub raises before touching them so dummy
        # values are fine.
        asyncio.run(
            proc(
                'x', None, None, {}, [],
                ('127.0.0.1', 0), {},
            )
        )

    msg: str = str(ei.value)
    assert 'main_thread_forkserver' in msg, (
        f'stub error msg should redirect to the working '
        f'variant-1 backend; got: {msg!r}'
    )
    assert 'msgspec#1026' in msg or '1026' in msg, (
        f'stub error msg should reference the upstream '
        f'blocker (jcrist/msgspec#1026); got: {msg!r}'
    )
