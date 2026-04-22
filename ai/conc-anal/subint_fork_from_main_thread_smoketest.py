#!/usr/bin/env python3
'''
Standalone CPython-level feasibility check for the "main-interp
worker-thread forkserver + subint-hosted trio" architecture
proposed as a workaround to the CPython-level refusal
documented in
`ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md`.

Purpose
-------
Deliberately NOT a `tractor` test. Zero `tractor` imports.
Uses `_interpreters` (private stdlib) + `os.fork()` directly so
the signal is unambiguous — pass/fail here is a property of
CPython alone, independent of our runtime.

Run each scenario in isolation; the child's fate is observable
only via `os.waitpid()` of the parent and the scenario's own
status prints.

Scenarios (pick one with `--scenario <name>`)
---------------------------------------------

- `control_subint_thread_fork` — the KNOWN-BROKEN case we
  documented in `subint_fork_blocked_by_cpython_post_fork_issue.md`:
  drive a subint from a thread, call `os.fork()` inside its
  `_interpreters.exec()`, watch the child abort. **Included as
  a control** — if this scenario DOESN'T abort the child, our
  analysis is wrong and we should re-check everything.

- `main_thread_fork` — baseline sanity. Call `os.fork()` from
  the process's main thread. Must always succeed; if this
  fails something much bigger is broken.

- `worker_thread_fork` — the architectural assertion. Spawn a
  regular `threading.Thread` (attached to main interp, NOT a
  subint), have IT call `os.fork()`. Child should survive
  post-fork cleanup.

- `full_architecture` — end-to-end: main-interp worker thread
  forks. In the child, fork-thread (still main-interp) creates
  a subint, drives a second worker thread inside it that runs
  a trivial `trio.run()`. Validates the "root runtime lives in
  a subint in the child" piece of the proposed arch.

All scenarios print a self-contained pass/fail banner. Exit
code 0 on expected outcome (which for `control_*` means "child
aborted", not "child succeeded"!).

Requires Python 3.14+.

Usage
-----
::

    python subint_fork_from_main_thread_smoketest.py \\
        --scenario main_thread_fork

    python subint_fork_from_main_thread_smoketest.py \\
        --scenario full_architecture

'''
from __future__ import annotations
import argparse
import os
import signal
import sys
import threading
import time
from typing import Callable


# Hard-require py3.14 for the public `concurrent.interpreters`
# API (we still drop to `_interpreters` internally, same as
# `tractor.spawn._subint`).
try:
    from concurrent import interpreters as _public_interpreters  # noqa: F401
    import _interpreters  # type: ignore
except ImportError:
    print(
        'FAIL (setup): requires Python 3.14+ '
        '(missing `concurrent.interpreters`)',
        file=sys.stderr,
    )
    sys.exit(2)


# ----------------------------------------------------------------
# small observability helpers
# ----------------------------------------------------------------


def _banner(title: str) -> None:
    line = '=' * 60
    print(f'\n{line}\n{title}\n{line}', flush=True)


def _wait_child(
    pid: int,
    *,
    label: str,
    expect_exit_ok: bool,
) -> bool:
    '''
    Await a forked child's exit status and render pass/fail.

    `expect_exit_ok=True` means we expect a normal exit (code
    0 via WEXITSTATUS). `expect_exit_ok=False` means we expect
    an abnormal death (WIFSIGNALED or nonzero WEXITSTATUS) —
    used for the `control_*` scenario where CPython is
    supposed to abort the child.

    '''
    _, status = os.waitpid(pid, 0)
    exited_normally = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    signaled = os.WIFSIGNALED(status)
    sig = os.WTERMSIG(status) if signaled else None
    rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None

    if expect_exit_ok:
        ok = exited_normally
        expected_str = 'normal exit (rc=0)'
    else:
        ok = not exited_normally
        expected_str = (
            'abnormal death (signal or nonzero exit)'
        )

    verdict = 'PASS' if ok else 'FAIL'
    status_str = (
        f'signal={signal.Signals(sig).name}'
        if signaled
        else f'rc={rc}'
    )
    print(
        f'[{verdict}] {label}: '
        f'expected {expected_str}; observed {status_str}',
        flush=True,
    )
    return ok


# ----------------------------------------------------------------
# scenario: `control_subint_thread_fork` (known-broken)
# ----------------------------------------------------------------


def scenario_control_subint_thread_fork() -> int:
    _banner(
        '[control] fork from INSIDE a subint (expected: child aborts)'
    )
    interp_id = _interpreters.create('legacy')
    print(f'  created subint {interp_id}', flush=True)

    # Shared flag: child writes a sentinel file we can detect from
    # the parent. If the child manages to write this, CPython's
    # post-fork refusal is NOT happening → analysis is wrong.
    sentinel = '/tmp/subint_fork_smoketest_control_child_ran'
    try:
        os.unlink(sentinel)
    except FileNotFoundError:
        pass

    bootstrap = (
        'import os\n'
        'pid = os.fork()\n'
        'if pid == 0:\n'
        # child — if CPython's refusal fires this code never runs
        f'    with open({sentinel!r}, "w") as f:\n'
        '        f.write("ran")\n'
        '    os._exit(0)\n'
        'else:\n'
        # parent side (inside the launchpad subint) — stash the
        # forked PID on a shareable dict so we can waitpid()
        # from the outer main interp. We can't just return it;
        # _interpreters.exec() returns nothing useful.
        '    import builtins\n'
        '    builtins._forked_child_pid = pid\n'
    )

    # NOTE, we can't easily pull state back from the subint.
    # For the CONTROL scenario we just time-bound the fork +
    # check the sentinel. If sentinel exists → child ran →
    # analysis wrong. If not → child aborted → analysis
    # confirmed.
    done = threading.Event()

    def _drive() -> None:
        try:
            _interpreters.exec(interp_id, bootstrap)
        except Exception as err:
            print(
                f'  subint bootstrap raised (expected on some '
                f'CPython versions): {type(err).__name__}: {err}',
                flush=True,
            )
        finally:
            done.set()

    t = threading.Thread(
        target=_drive,
        name='control-subint-fork-launchpad',
        daemon=True,
    )
    t.start()
    done.wait(timeout=5.0)
    t.join(timeout=2.0)

    # Give the (possibly-aborted) child a moment to die.
    time.sleep(0.5)

    sentinel_present = os.path.exists(sentinel)
    verdict = (
        # "PASS" for our analysis means sentinel NOT present.
        'PASS' if not sentinel_present else 'FAIL (UNEXPECTED)'
    )
    print(
        f'[{verdict}] control: sentinel present={sentinel_present} '
        f'(analysis predicts False — child should abort before '
        f'writing)',
        flush=True,
    )
    if sentinel_present:
        os.unlink(sentinel)

    try:
        _interpreters.destroy(interp_id)
    except _interpreters.InterpreterError:
        pass

    return 0 if not sentinel_present else 1


# ----------------------------------------------------------------
# scenario: `main_thread_fork` (baseline sanity)
# ----------------------------------------------------------------


def scenario_main_thread_fork() -> int:
    _banner(
        '[baseline] fork from MAIN thread (expected: child exits normally)'
    )

    pid = os.fork()
    if pid == 0:
        os._exit(0)

    return 0 if _wait_child(
        pid,
        label='main_thread_fork',
        expect_exit_ok=True,
    ) else 1


# ----------------------------------------------------------------
# scenario: `worker_thread_fork` (architectural assertion)
# ----------------------------------------------------------------


def _fork_from_worker_thread(
    child_target: Callable[[], int] | None = None,
    label: str = 'worker_thread_fork',
) -> int:
    '''
    Fork from a main-interp worker thread (not a subint).
    Returns the child's exit code observed by the parent.

    `child_target` is called IN THE CHILD before `os._exit`.
    If omitted, the child just `_exit(0)`s immediately.

    `label` is used in the pass/fail banner so reuse of this
    helper across scenarios reports the scenario name, not
    just the underlying fork-mechanism name.

    '''
    # Use a simple pipe to shuttle the child PID back to main.
    rfd, wfd = os.pipe()

    def _worker() -> None:
        pid = os.fork()
        if pid == 0:
            # CHILD: close parent's pipe ends, do work, exit.
            os.close(rfd)
            os.close(wfd)
            rc = 0
            if child_target is not None:
                try:
                    rc = child_target() or 0
                except BaseException as err:
                    print(
                        f'  CHILD: child_target raised: '
                        f'{type(err).__name__}: {err}',
                        file=sys.stderr, flush=True,
                    )
                    rc = 2
            os._exit(rc)
        else:
            # PARENT (still in worker thread): send pid to
            # main thread via the pipe.
            os.write(wfd, pid.to_bytes(8, 'little'))

    t = threading.Thread(
        target=_worker,
        name=f'worker-fork-thread[{label}]',
        daemon=False,
    )
    t.start()
    t.join(timeout=10.0)
    if t.is_alive():
        print(
            f'[FAIL] {label}: worker-thread fork driver '
            f'did not return in 10s',
            flush=True,
        )
        return 1

    pid_bytes = os.read(rfd, 8)
    os.close(rfd)
    os.close(wfd)
    pid = int.from_bytes(pid_bytes, 'little')
    print(f'  forked child pid={pid}', flush=True)

    return 0 if _wait_child(
        pid,
        label=label,
        expect_exit_ok=True,
    ) else 1


def scenario_worker_thread_fork() -> int:
    _banner(
        '[arch] fork from MAIN-INTERP WORKER thread '
        '(expected: child exits normally — this is the one '
        'that matters)'
    )
    return _fork_from_worker_thread(
        child_target=None,
        label='worker_thread_fork',
    )


# ----------------------------------------------------------------
# scenario: `full_architecture`
# ----------------------------------------------------------------


def _child_trio_in_subint() -> int:
    '''
    CHILD-side: from fork-thread (main-interp), create a fresh
    subint and run `trio.run()` in it on a dedicated worker
    thread. Returns 0 on success.
    '''
    child_interp = _interpreters.create('legacy')
    subint_bootstrap = (
        'import trio\n'
        'async def _main():\n'
        '    await trio.sleep(0.05)\n'
        '    return 42\n'
        'result = trio.run(_main)\n'
        'assert result == 42, f"trio.run returned {result}"\n'
        'print("  CHILD subint: trio.run OK, result=42", '
        'flush=True)\n'
    )
    err = None

    def _drive() -> None:
        nonlocal err
        try:
            _interpreters.exec(child_interp, subint_bootstrap)
        except BaseException as e:
            err = e

    t = threading.Thread(
        target=_drive,
        name='child-subint-trio-thread',
        daemon=False,
    )
    t.start()
    t.join(timeout=10.0)

    try:
        _interpreters.destroy(child_interp)
    except _interpreters.InterpreterError:
        pass

    if t.is_alive():
        print(
            '  CHILD: subint trio thread did not return in 10s',
            flush=True,
        )
        return 3
    if err is not None:
        print(
            f'  CHILD: subint bootstrap raised: '
            f'{type(err).__name__}: {err}',
            flush=True,
        )
        return 4
    return 0


def scenario_full_architecture() -> int:
    _banner(
        '[arch-full] worker-thread fork + child runs trio in a '
        'subint (end-to-end proposed arch)'
    )
    return _fork_from_worker_thread(
        child_target=_child_trio_in_subint,
        label='full_architecture',
    )


# ----------------------------------------------------------------
# main
# ----------------------------------------------------------------


SCENARIOS: dict[str, Callable[[], int]] = {
    'control_subint_thread_fork': scenario_control_subint_thread_fork,
    'main_thread_fork': scenario_main_thread_fork,
    'worker_thread_fork': scenario_worker_thread_fork,
    'full_architecture': scenario_full_architecture,
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        '--scenario',
        choices=sorted(SCENARIOS.keys()),
        required=True,
    )
    args = ap.parse_args()
    return SCENARIOS[args.scenario]()


if __name__ == '__main__':
    sys.exit(main())
