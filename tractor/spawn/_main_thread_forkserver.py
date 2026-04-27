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
Fork-from-main-interp-worker-thread primitives.

Generic, tractor-spawn-backend-agnostic primitives for forking a
child OS process via `os.fork()` from a regular `threading.Thread`
attached to the main CPython interpreter. Builds the lowest layer
that any "subint forkserver"-style spawn backend wants to compose
on top of.

Why this module exists
----------------------

Two empirical CPython properties drive the design:

1. **`os.fork()` from a non-main sub-interpreter is refused by
   CPython.** `PyOS_AfterFork_Child()` →
   `_PyInterpreterState_DeleteExceptMain()` gates on the calling
   thread's tstate belonging to the main interpreter and aborts
   the forked child otherwise (`Fatal Python error: not main
   interpreter`). Full source-level walkthrough:
   `ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md`.
2. **`os.fork()` from a regular `threading.Thread` attached to
   the *main* interpreter — i.e. a worker thread that has never
   entered a subint — works cleanly.** Empirically validated
   across four scenarios by
   `ai/conc-anal/subint_fork_from_main_thread_smoketest.py` on
   py3.14.

This module provides the working primitive set: spawn a worker
thread, fork in it, retrieve the child pid back to the caller
trio task, and offer a `trio.Process`-shaped shim around the raw
pid so the existing `soft_kill`/`hard_reap` patterns from
`_spawn.py` keep working unchanged.

Companion module
----------------

`tractor.spawn._subint_forkserver` builds tractor's
`subint_forkserver` spawn backend on top of these primitives —
the spawn-backend coroutine, the subint-specific `proc_kwargs`
handling, the `_actor_child_main` invocation in the fork-child,
and the eventual subint-hosted-trio-runtime arch (gated on
[jcrist/msgspec#1026](https://github.com/jcrist/msgspec/issues/1026)).
That module imports only the pieces it needs from here.

What lives here vs. there
-------------------------

Here (truly generic, no tractor or subint dep):

- `_close_inherited_fds()`   — fd hygiene primitive
- `_format_child_exit()`     — `waitpid()` status renderer
- `wait_child()`             — synchronous waitpid wrapper
- `fork_from_worker_thread()` — the core fork primitive
- `_ForkedProc`              — trio-cancellable child-wait shim

There (tractor-specific):

- `run_subint_in_worker_thread()` — subint primitive (companion
  to `fork_from_worker_thread` for the future-arch where the
  parent's trio runs in a subint)
- `subint_forkserver_proc()`      — the spawn-backend coroutine
  itself (SpawnSpec handshake, IPC wiring, lifecycle)

See also
--------

- `tractor.spawn._subint_fork` — the stub for the
  fork-from-non-main-subint strategy that DIDN'T work (kept
  in-tree as documentation of the attempt + the CPython-level
  block).
- `ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md`
  — CPython source walkthrough of why fork-from-subint is dead.
- `ai/conc-anal/subint_fork_from_main_thread_smoketest.py`
  — standalone feasibility check (delegates to this module
  for the primitives it exercises).

'''
from __future__ import annotations
import os
import signal
import threading
from typing import Callable

import trio

from tractor.log import get_logger


log = get_logger('tractor')


def _close_inherited_fds(
    keep: frozenset[int] = frozenset({0, 1, 2}),
) -> int:
    '''
    Close every open file descriptor in the current process
    EXCEPT those in `keep` (default: stdio only).

    Intended as the first thing a post-`os.fork()` child runs
    after closing any communication pipes it knows about. This
    is the fork-child FD hygiene discipline that
    `subprocess.Popen(close_fds=True)` applies by default for
    its exec-based children, but which we have to implement
    ourselves because our `fork_from_worker_thread()` primitive
    deliberately does NOT exec.

    Why it matters
    --------------
    Without this, a forkserver-spawned subactor inherits the
    parent actor's IPC listener sockets, trio-epoll fd, trio
    wakeup-pipe, peer-channel sockets, etc. If that subactor
    then itself forkserver-spawns a grandchild, the grandchild
    inherits the FDs transitively from *both* its direct
    parent AND the root actor — IPC message routing becomes
    ambiguous and the cancel cascade deadlocks. See
    `ai/conc-anal/subint_forkserver_test_cancellation_leak_issue.md`
    for the full diagnosis + the empirical repro.

    Fresh children will open their own IPC sockets via
    `_actor_child_main()`, so they don't need any of the
    parent's FDs.

    Returns the count of fds that were successfully closed —
    useful for sanity-check logging at callsites.

    '''
    # Enumerate open fds via `/proc/self/fd` on Linux (the fast +
    # precise path); fall back to `RLIMIT_NOFILE` range close on
    # other platforms. Matches stdlib
    # `subprocess._posixsubprocess.close_fds` strategy.
    try:
        fd_names: list[str] = os.listdir('/proc/self/fd')
        candidates: list[int] = [
            int(n) for n in fd_names if n.isdigit()
        ]
    except (
        FileNotFoundError,
        PermissionError,
    ):
        import resource
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        candidates = list(range(3, soft))

    closed: int = 0
    for fd in candidates:
        if fd in keep:
            continue
        try:
            os.close(fd)
            closed += 1
        except OSError:
            # fd was already closed (race with listdir) or otherwise
            # unclosable — either is fine.
            log.exception(
                f'Failed to close inherited fd in child ??\n'
                f'{fd!r}\n'
            )

    return closed


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
    thread_name: str = 'main-thread-fork',
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
            # them here), then scrub ALL other inherited FDs
            # so the child starts with a clean slate
            # (stdio-only). Critical for multi-level spawn
            # trees — see `_close_inherited_fds()` docstring.
            os.close(rfd)
            os.close(wfd)
            _close_inherited_fds()
            rc: int = 0
            if child_target is not None:
                try:
                    rc = child_target() or 0
                except BaseException as err:
                    log.error(
                        f'main-thread-fork child_target '
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
            log.exception(
                f'Failed to close PID-pipe read-fd in parent ??\n'
                f'{rfd!r}\n'
            )
        try:
            os.close(wfd)
        except OSError:
            log.exception(
                f'Failed to close PID-pipe write-fd in parent ??\n'
                f'{wfd!r}\n'
            )
        raise RuntimeError(
            f'main-thread-fork worker thread '
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
        f'main-thread-fork forked child\n'
        f'(>\n'
        f' |_pid={pid}\n'
    )
    return pid


class _ForkedProc:
    '''
    Thin `trio.Process`-compatible shim around a raw OS pid
    returned by `fork_from_worker_thread()`, exposing just
    enough surface for the `soft_kill()` / hard-reap pattern
    borrowed from `trio_proc()`.

    Unlike `trio.Process`, we have no direct handles on the
    child's std-streams (fork-without-exec inherits the
    parent's FDs, but we don't marshal them into this
    wrapper) — `.stdin`/`.stdout`/`.stderr` are all `None`,
    which matches what `soft_kill()` handles via its
    `is not None` guards.

    '''
    def __init__(self, pid: int):
        self.pid: int = pid
        self._returncode: int | None = None
        # `soft_kill`/`hard_kill` check these for pipe
        # teardown — all None since we didn't wire up pipes
        # on the fork-without-exec path.
        self.stdin = None
        self.stdout = None
        self.stderr = None
        # pidfd (Linux 5.3+, Python 3.9+) — a file descriptor
        # referencing this child process which becomes readable
        # once the child exits. Enables a fully trio-cancellable
        # wait via `trio.lowlevel.wait_readable()` — same
        # pattern `trio.Process.wait()` uses under the hood, and
        # the same pattern `multiprocessing.Process.sentinel`
        # uses for `tractor.spawn._spawn.proc_waiter()`. Without
        # this, waiting via `trio.to_thread.run_sync(os.waitpid,
        # ...)` blocks a cache thread on a sync syscall that is
        # NOT trio-cancellable, which prevents outer cancel
        # scopes from unwedging a stuck-child cancel cascade.
        self._pidfd: int = os.pidfd_open(pid)

    def poll(self) -> int | None:
        '''
        Non-blocking liveness probe. Returns `None` if the
        child is still running, else its exit code (negative
        for signal-death, matching `subprocess.Popen`
        convention).

        '''
        if self._returncode is not None:
            return self._returncode
        try:
            waited_pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            # already reaped (or never existed) — treat as
            # clean exit for polling purposes.
            self._returncode = 0
            return 0
        if waited_pid == 0:
            return None
        self._returncode = self._parse_status(status)
        return self._returncode

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        '''
        Async, fully-trio-cancellable wait for the child's
        exit. Uses `trio.lowlevel.wait_readable()` on the
        `pidfd` sentinel — same pattern as `trio.Process.wait`
        and `tractor.spawn._spawn.proc_waiter` (mp backend).

        Safe to call multiple times; subsequent calls return
        the cached rc without re-issuing the syscall.

        '''
        if self._returncode is not None:
            return self._returncode
        # Park until the pidfd becomes readable — the OS
        # signals this exactly once on child exit. Cancellable
        # via any outer trio cancel scope (this was the key
        # fix vs. the prior `to_thread.run_sync(os.waitpid,
        # abandon_on_cancel=False)` which blocked a thread on
        # a sync syscall and swallowed cancels).
        await trio.lowlevel.wait_readable(self._pidfd)
        # pidfd signaled → reap non-blocking to collect the
        # exit status. `WNOHANG` here is correct: by the time
        # the pidfd is readable, `waitpid()` won't block.
        try:
            _, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            # already reaped by something else
            status = 0
        self._returncode = self._parse_status(status)
        # pidfd is one-shot; close it so we don't leak fds
        # across many spawns.
        try:
            os.close(self._pidfd)
        except OSError:
            pass
        self._pidfd = -1
        return self._returncode

    def kill(self) -> None:
        '''
        OS-level `SIGKILL` to the child. Swallows
        `ProcessLookupError` (already dead).

        '''
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def __del__(self) -> None:
        # belt-and-braces: close the pidfd if `wait()` wasn't
        # called (e.g. unexpected teardown path).
        fd: int = getattr(self, '_pidfd', -1)
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def _parse_status(self, status: int) -> int:
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            # negative rc by `subprocess.Popen` convention
            return -os.WTERMSIG(status)
        return 0

    def __repr__(self) -> str:
        return (
            f'<_ForkedProc pid={self.pid} '
            f'returncode={self._returncode}>'
        )
