'''
Integration exercises for the `tractor.spawn._subint_forkserver`
submodule at three tiers:

1. the low-level primitives
   (`fork_from_worker_thread()` +
   `run_subint_in_worker_thread()`) driven from inside a real
   `trio.run()` in the parent process,

2. the full `subint_forkserver_proc` spawn backend wired
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
`subint_forkserver` spawn backend end-to-end against the
tractor runtime.

Gating
------
- py3.14+ (via `concurrent.interpreters` presence)
- no `--spawn-backend` restriction — the backend-level test
  flips `tractor.spawn._spawn._spawn_method` programmatically
  (via `try_set_start_method('subint_forkserver')`) and
  restores it on teardown, so these tests are independent of
  the session-level CLI backend choice.

'''
from __future__ import annotations
from functools import partial
import os

import pytest
import trio

import tractor
from tractor.devx import dump_on_hang


# Gate: subint forkserver primitives require py3.14+. Check
# the public stdlib wrapper's presence (added in 3.14) rather
# than `_interpreters` directly — see
# `tractor.spawn._subint` for why.
pytest.importorskip('concurrent.interpreters')

from tractor.spawn._subint_forkserver import (  # noqa: E402
    fork_from_worker_thread,
    run_subint_in_worker_thread,
    wait_child,
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
@pytest.mark.timeout(30, method='thread')
def test_fork_from_worker_thread_via_trio() -> None:
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
        path='/tmp/subint_forkserver_baseline.dump',
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
    pattern the proposed `subint_forkserver` spawn backend
    would rest on.

    '''
    deadline: float = 15.0
    with dump_on_hang(
        seconds=deadline,
        path='/tmp/subint_forkserver_trio_in_child.dump',
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
# tier-3 backend test: drive the registered `subint_forkserver`
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
    `subint_forkserver`), run a trivial RPC through its
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
    `'subint_forkserver'` for the duration of a test, then
    restore whatever was in place before (usually the
    session-level CLI choice, typically `'trio'`).

    Without this, other tests in the same session would
    observe the global flip and start spawning via fork —
    which is almost certainly NOT what their assertions were
    written against.

    '''
    prev_method: str = _spawn_mod._spawn_method
    prev_ctx = _spawn_mod._ctx
    try_set_start_method('subint_forkserver')
    try:
        yield
    finally:
        _spawn_mod._spawn_method = prev_method
        _spawn_mod._ctx = prev_ctx


@pytest.mark.timeout(60, method='thread')
def test_subint_forkserver_spawn_basic(
    reg_addr: tuple[str, int | str],
    forkserver_spawn_method,
) -> None:
    '''
    Happy-path: spawn ONE subactor via the
    `subint_forkserver` backend (parent-side fork from a
    main-interp worker thread), do a trivial portal-RPC
    round-trip, tear the nursery down cleanly.

    If this passes, the "forkserver + tractor runtime" arch
    is proven end-to-end: the registered
    `subint_forkserver_proc` spawn target successfully
    forks a child, the child runs `_actor_child_main()` +
    completes IPC handshake + serves an RPC, and the parent
    reaps via `_ForkedProc.wait()` without regressing any of
    the normal nursery teardown invariants.

    '''
    deadline: float = 20.0
    with dump_on_hang(
        seconds=deadline,
        path='/tmp/subint_forkserver_spawn_basic.dump',
    ):
        trio.run(
            partial(
                _happy_path_forkserver,
                reg_addr,
                deadline,
            ),
        )
