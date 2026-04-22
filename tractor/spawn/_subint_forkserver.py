# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
Forkserver-style `os.fork()` primitives for the `subint`-hosted
actor model.

Background
----------
CPython refuses `os.fork()` from a non-main sub-interpreter:
`PyOS_AfterFork_Child()` →
`_PyInterpreterState_DeleteExceptMain()` gates on the calling
thread's tstate belonging to the main interpreter and aborts
the forked child otherwise. The full walkthrough (with source
refs) lives in
`ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md`.

However `os.fork()` from a regular `threading.Thread` attached
to the *main* interpreter — i.e. a worker thread that has
never entered a subint — works cleanly. Empirically validated
across four scenarios by
`ai/conc-anal/subint_fork_from_main_thread_smoketest.py` on
py3.14.

This submodule lifts the validated primitives out of the
smoke-test and into tractor proper, so they can eventually be
wired into a real "subint forkserver" spawn backend — where:

- A dedicated main-interp worker thread owns all `os.fork()`
  calls (never enters a subint).
- The tractor parent-actor's `trio.run()` lives in a
  sub-interpreter on a different worker thread.
- When a spawn is requested, the trio-task signals the
  forkserver thread; the forkserver forks; child re-enters
  the same pattern (trio in a subint + forkserver on main).

This mirrors the stdlib `multiprocessing.forkserver` design
but keeps the forkserver in-process for faster spawn latency
and inherited parent state.

Status
------
**EXPERIMENTAL** — primitives only. Not yet wired into
`tractor.spawn._spawn`'s backend registry. The next step is
to drive these from a parent-side `trio.run()` and hook the
returned child pid into tractor's normal actor-nursery/IPC
machinery.

TODO — cleanup gated on msgspec PEP 684 support
-----------------------------------------------
Both primitives below allocate a dedicated
`threading.Thread` rather than using
`trio.to_thread.run_sync()`. That's a cautious design
rooted in three distinct-but-entangled issues (GIL
starvation from legacy-config subints, tstate-recycling
destroy race on trio cache threads, fork-from-main-tstate
invariant). Some of those dissolve under PEP 684
isolated-mode subints; one requires empirical re-testing
to know.

Full analysis + audit plan for when we can revisit is in
`ai/conc-anal/subint_forkserver_thread_constraints_on_pep684_issue.md`.
Intent: file a follow-up GH issue linked to #379 once
[jcrist/msgspec#563](https://github.com/jcrist/msgspec/issues/563)
unblocks isolated-mode subints in tractor.

See also
--------
- `tractor.spawn._subint_fork` — the stub for the
  fork-from-subint strategy that DIDN'T work (kept as
  in-tree documentation of the attempt + CPython-level
  block).
- `ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md`
  — the CPython source walkthrough.
- `ai/conc-anal/subint_fork_from_main_thread_smoketest.py`
  — the standalone feasibility check (now delegates to
  this module for the primitives it exercises).

'''
from __future__ import annotations
import os
import signal
import threading
from typing import (
    Callable,
    TYPE_CHECKING,
)

from tractor.log import get_logger

if TYPE_CHECKING:
    pass


log = get_logger('tractor')


# Feature-gate: py3.14+ via the public `concurrent.interpreters`
# wrapper. Matches the gate in `tractor.spawn._subint` —
# see that module's docstring for why we require the public
# API's presence even though we reach into the private
# `_interpreters` C module for actual calls.
try:
    from concurrent import interpreters as _public_interpreters  # noqa: F401  # type: ignore
    import _interpreters  # type: ignore
    _has_subints: bool = True
except ImportError:
    _interpreters = None  # type: ignore
    _has_subints: bool = False


def _format_child_exit(
    status: int,
) -> str:
    '''
    Render `os.waitpid()`-returned status as a short human
    string (`'rc=0'` / `'signal=SIGABRT'` / etc.) for log
    output.

    '''
    if os.WIFEXITED(status):
        return f'rc={os.WEXITSTATUS(status)}'
    elif os.WIFSIGNALED(status):
        sig: int = os.WTERMSIG(status)
        return f'signal={signal.Signals(sig).name}'
    else:
        return f'raw_status={status}'


def wait_child(
    pid: int,
    *,
    expect_exit_ok: bool = True,
) -> tuple[bool, str]:
    '''
    `os.waitpid()` + classify the child's exit as
    expected-or-not.

    `expect_exit_ok=True` → expect clean `rc=0`. `False` →
    expect abnormal death (any signal or nonzero rc). Used
    by the control-case smoke-test scenario where CPython
    is meant to abort the child.

    Returns `(ok, status_str)` — `ok` reflects whether the
    observed outcome matches `expect_exit_ok`, `status_str`
    is a short render of the actual status.

    '''
    _, status = os.waitpid(pid, 0)
    exited_normally: bool = (
        os.WIFEXITED(status)
        and
        os.WEXITSTATUS(status) == 0
    )
    ok: bool = (
        exited_normally
        if expect_exit_ok
        else not exited_normally
    )
    return ok, _format_child_exit(status)


def fork_from_worker_thread(
    child_target: Callable[[], int] | None = None,
    *,
    thread_name: str = 'subint-forkserver',
    join_timeout: float = 10.0,

) -> int:
    '''
    `os.fork()` from a main-interp worker thread; return the
    forked child's pid.

    The calling context **must** be the main interpreter
    (not a subinterpreter) — that's the whole point of this
    primitive. A regular `threading.Thread(target=...)`
    spawned from main-interp code satisfies this
    automatically because Python attaches the thread's
    tstate to the *calling* interpreter, and our main
    thread's calling interp is always main.

    If `child_target` is provided, it runs IN the forked
    child process before `os._exit` is called. The callable
    should return an int used as the child's exit rc. If
    `child_target` is None, the child `_exit(0)`s immediately
    (useful for the baseline sanity case).

    On the PARENT side, this function drives the worker
    thread to completion (`fork()` returns near-instantly;
    the thread is expected to exit promptly) and then
    returns the forked child's pid. Raises `RuntimeError`
    if the worker thread fails to return within
    `join_timeout` seconds — that'd be an unexpected CPython
    pathology.

    '''
    if not _has_subints:
        raise RuntimeError(
            'subint-forkserver primitives require Python '
            '3.14+ (public `concurrent.interpreters` module '
            'not present on this runtime).'
        )

    # Use a pipe to shuttle the forked child's pid from the
    # worker thread back to the caller.
    rfd, wfd = os.pipe()

    def _worker() -> None:
        '''
        Runs on the forkserver worker thread. Forks; child
        runs `child_target` (if any) and exits; parent side
        writes the child pid to the pipe so the main-thread
        caller can retrieve it.

        '''
        pid: int = os.fork()
        if pid == 0:
            # CHILD: close the pid-pipe ends (we don't use
            # them here), run the user callable if any, exit.
            os.close(rfd)
            os.close(wfd)
            rc: int = 0
            if child_target is not None:
                try:
                    rc = child_target() or 0
                except BaseException as err:
                    log.error(
                        f'subint-forkserver child_target '
                        f'raised:\n'
                        f'|_{type(err).__name__}: {err}'
                    )
                    rc = 2
            os._exit(rc)
        else:
            # PARENT (still inside the worker thread):
            # hand the child pid back to main via pipe.
            os.write(wfd, pid.to_bytes(8, 'little'))

    worker: threading.Thread = threading.Thread(
        target=_worker,
        name=thread_name,
        daemon=False,
    )
    worker.start()
    worker.join(timeout=join_timeout)
    if worker.is_alive():
        # Pipe cleanup best-effort before bail.
        try:
            os.close(rfd)
        except OSError:
            pass
        try:
            os.close(wfd)
        except OSError:
            pass
        raise RuntimeError(
            f'subint-forkserver worker thread '
            f'{thread_name!r} did not return within '
            f'{join_timeout}s — this is unexpected since '
            f'`os.fork()` should return near-instantly on '
            f'the parent side.'
        )

    pid_bytes: bytes = os.read(rfd, 8)
    os.close(rfd)
    os.close(wfd)
    pid: int = int.from_bytes(pid_bytes, 'little')
    log.runtime(
        f'subint-forkserver forked child\n'
        f'(>\n'
        f' |_pid={pid}\n'
    )
    return pid


def run_subint_in_worker_thread(
    bootstrap: str,
    *,
    thread_name: str = 'subint-trio',
    join_timeout: float = 10.0,

) -> None:
    '''
    Create a fresh legacy-config sub-interpreter and drive
    the given `bootstrap` code string through
    `_interpreters.exec()` on a dedicated worker thread.

    Naming mirrors `fork_from_worker_thread()`:
    "<action>_in_worker_thread" — the action here is "run a
    subint", not "run trio" per se. Typical `bootstrap`
    content does import `trio` + call `trio.run()`, but
    nothing about this primitive requires trio; it's a
    generic "host a subint on a worker thread" helper.
    Intended mainly for use inside a fork-child (see
    `tractor.spawn._subint_forkserver` module docstring) but
    works anywhere.

    See `tractor.spawn._subint.subint_proc` for the matching
    pattern tractor uses at the sub-actor level.

    Destroys the subint after the thread joins.

    '''
    if not _has_subints:
        raise RuntimeError(
            'subint-forkserver primitives require Python '
            '3.14+.'
        )

    interp_id: int = _interpreters.create('legacy')
    log.runtime(
        f'Created child-side subint for trio.run()\n'
        f'(>\n'
        f' |_interp_id={interp_id}\n'
    )

    err: BaseException | None = None

    def _drive() -> None:
        nonlocal err
        try:
            _interpreters.exec(interp_id, bootstrap)
        except BaseException as e:
            err = e

    worker: threading.Thread = threading.Thread(
        target=_drive,
        name=thread_name,
        daemon=False,
    )
    worker.start()
    worker.join(timeout=join_timeout)

    try:
        _interpreters.destroy(interp_id)
    except _interpreters.InterpreterError as e:
        log.warning(
            f'Could not destroy child-side subint '
            f'{interp_id}: {e}'
        )

    if worker.is_alive():
        raise RuntimeError(
            f'child-side subint trio-driver thread '
            f'{thread_name!r} did not return within '
            f'{join_timeout}s.'
        )
    if err is not None:
        raise err
