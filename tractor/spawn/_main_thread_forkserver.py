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
Variant-1 "main-thread forkserver" spawn backend (today's
working impl) + the generic fork-from-main-interp-worker-thread
primitives it's built on.

Spawn-method key: `'main_thread_forkserver'`. The legacy
`'subint_forkserver'` key currently aliases here too — see
`tractor.spawn._subint_forkserver` for the future variant-2
(subint-isolated-child runtime, gated on
[jcrist/msgspec#1026](https://github.com/jcrist/msgspec/issues/1026))
that key is reserved for.

Background
----------

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

The fork-from-main-thread primitives below codify property (2)
into a reusable surface: spawn a worker thread, fork in it,
retrieve the child pid back to the caller trio task, and offer a
`trio.Process`-shaped shim around the raw pid so the existing
`soft_kill`/`hard_reap` patterns from `_spawn.py` keep working
unchanged.

Design rationale — why a forkserver, and why in-process
-------------------------------------------------------

Two design questions worth pinning down up front, since the
naming intentionally evokes the stdlib `multiprocessing.forkserver`
for comparison:

**(1) Why a forkserver pattern at all, vs. forking directly
from the trio task?**

`os.fork()` is fundamentally hostile to trio: trio owns
file descriptors, signal-wakeup-fds, threadpools, and an
event loop with non-trivial post-fork lifecycle invariants
(see python-trio/trio#1614 et al.). Forking a trio-running
thread duplicates all that state into the child, which then
either needs surgical reset (fragile) or has to immediately
`exec()` (defeats the point of fork-without-exec). The
*forkserver* sidesteps this by isolating the `os.fork()`
call in a worker that has provably never entered trio — so
the child inherits a clean, trio-free image.

**(2) Why an in-process forkserver, vs. stdlib
`multiprocessing.forkserver`?**

The stdlib design solves the same "fork from clean state"
problem by spinning up a **separate sidecar process** at
first use of `mp.set_start_method('forkserver')`. The parent
then IPC's each spawn request to that sidecar over a unix
socket; the sidecar is the process that actually calls
`os.fork()`. This works but pays for cleanliness with three
costs:

- **Sidecar lifecycle**: a second long-lived process per
  parent, with its own start/stop/health-check semantics.

- **IPC overhead per spawn**: every actor-spawn round-trips
  an `mp` request message through a unix socket before any
  child code runs.

- **State isolation by process boundary**: the sidecar can't
  share parent state at all — every spawn is a "cold" child
  re-importing modules from disk.

Once the variant-2 (subint-isolated child runtime) lands the
in-process forkserver collapses all three costs:

- no sidecar — the forkserver is just another thread,
- spawn signal is a thread-local event/condition, not IPC,
- child inherits the warm parent state (loaded modules,
  populated caches, etc.) for free.

For the full variant-2 picture see
`tractor.spawn._subint_forkserver`'s docstring. Today (variant
1) we already get costs 1 + 2 collapsed; cost 3 will land
when msgspec#1026 unblocks isolated-mode subints.


What survives the fork? — POSIX semantics
-----------------------------------------

A natural worry when forking from a parent that's running
`trio.run()` on another thread: does that trio thread (and
any other threads in the parent) keep running in the child?

**No** — but with a precise meaning that's worth pinning
down, since the canonical trio framing
([python-trio/trio#1614](https://github.com/python-trio/trio/issues/1614))
puts it the opposite-sounding way:

> If you use `fork()` in a process with multiple threads,
> all the other thread stacks are just leaked: there's
> nothing else you can reasonably do with them.

Both statements describe the same POSIX reality from
opposite sides:

- **Execution-side ("gone")**: POSIX `fork()` only
  preserves the *calling* thread as a runnable thread in
  the child. Every other thread in the parent — trio's
  runner thread, any `to_thread` cache threads, anything
  else — never executes another instruction post-fork.

- **Memory-side ("leaked")**: those non-running threads'
  *stacks* and per-thread heap structures are still
  COW-inherited into the child's address space. They
  persist as orphaned bytes with no owning thread, no
  scheduler entry, and no way for the child to clean
  them up — hence trio's word "leaked".

Concretely, after the forkserver worker calls `os.fork()`:

| thread              | parent    | child (executing) | child (memory)              |
|---------------------|-----------|-------------------|-----------------------------|
| forkserver worker   | continues | sole survivor     | live stack                  |
| `trio.run()` thread | continues | not running       | leaked stack (zombie bytes) |
| any other thread    | continues | not running       | leaked stack (zombie bytes) |

The forkserver worker becomes the new "main" execution
context in the child; `trio.run()` and every other parent
thread never executes a single instruction post-fork.
Their stack memory rides along as inert COW pages until
the child's fresh `trio.run()` boots and overwrites/GCs
it (or until the child `exec()`s and discards the entire
image).

This is exactly *why* `os.fork()` is delegated to a
dedicated worker thread that has provably never entered
trio: we want that trio-free thread to be the surviving
*executing* thread in the child, with the leaked trio
stack reduced to inert COW pages we don't touch.

The leaked-stack residue is one slice of the broader
"fork in a multithreaded program is dangerous" hazard
class (see `man pthread_atfork`). Other dead-thread
artifacts that cross the fork boundary, and how we handle
each:

- **Inherited file descriptors** — the dead trio thread's
  epoll fd, signal-wakeup-fd, eventfds, sockets, IPC
  pipes, pytest's capture-fds, etc. are all still in the
  child's fd table (kernel-level inheritance). Handled by
  `_close_inherited_fds()` in the child prelude — walks
  `/proc/self/fd` and closes everything except stdio +
  the channel pipe to the forkserver.

- **Memory image** — trio's internal data structures
  (scheduler, task queues, runner state) sit in COW
  memory alongside the leaked stacks above. Nobody's
  executing them; they get GC'd / overwritten when the
  child's fresh `trio.run()` boots.

- **Python thread state** — handled automatically by
  CPython. `PyOS_AfterFork_Child()` calls
  `_PyThreadState_DeleteExceptCurrent()`, so dead
  `PyThreadState` objects are cleaned and
  `threading.enumerate()` returns just the surviving
  thread.

- **User-level locks (`threading.Lock`)** —
  held-by-dead-thread state is the canonical fork hazard.
  Not an issue in practice for tractor: trio doesn't hold
  cross-thread locks across fork (its synchronization is
  within the trio task system, which doesn't survive in
  either direction). CPython's GIL is auto-reset by the
  fork callback.


FYI: how this dodges the `trio.run()` × `fork()` hazards
--------------------------------------------------------

`os.fork()` is famously hostile to `trio` (see
python-trio/trio#1614 et al.) because trio owns several
classes of process-global state that all break across the
fork boundary in different ways. The forkserver-thread
design dodges each class explicitly:

- **Signal-wakeup-fd**: trio installs a wakeup-fd via
  `signal.set_wakeup_fd()` on `trio.run()` startup so
  signals can interrupt `epoll_wait`. The child inherits
  this fd, but trio's runner that owns it is gone — so
  any signal delivery in the child writes to a dead
  reader. *Dodge*: the inherited wakeup-fd is closed by
  `_close_inherited_fds()`, then the child's own
  `trio.run()` installs a fresh one.

- **`epoll`/`kqueue` instance**: trio's I/O backend holds
  one. Inherited as a dead fd; same fix as above.

- **Threadpool cache threads** (`trio.to_thread`): worker
  threads with cached tstate. Don't exist in the child
  (POSIX); cache state is meaningless garbage that gets
  reset when the child's trio.run() initializes its own
  thread cache.

- **Cancel scopes / nurseries / open `trio.Process` /
  open sockets**: these are trio-runtime objects, not
  kernel objects. The runtime that owns them is gone in
  the child, so the Python objects exist as zombie data
  in COW memory and get overwritten as the child runs.
  Inherited *kernel* fds those objects wrapped (sockets,
  proc pipes) are caught by `_close_inherited_fds()`.

- **`atexit` handlers**: trio doesn't register any that
  would mis-fire post-fork; trio's lifetime-stack is
  all `with`-block-scoped and dies with the runner.

- **Foreign-language I/O state** (libcurl, OpenSSL session
  caches, etc.): out of scope — same hazard as any
  fork-without-exec; users layering those on top of
  tractor need their own pthread_atfork handlers.

Net effect: for the runtime surface tractor controls
(trio + IPC layer + msgspec), the forkserver-thread
isolation + `_close_inherited_fds()` cleanup gives the
forked child a clean trio environment. Everything else
falls under the standard fork-without-exec disclaimer.


Implementation status
---------------------

- A dedicated main-interp worker thread owns all `os.fork()`
  calls (never enters a subint). ✓ landed.
- Parent actor's `trio.run()` lives **on the main interp**
  for now (not a subint yet). The subint-hosted root
  runtime is the variant-2 step gated on jcrist/msgspec#1026.
- Spawn-request signal: trio task `→ to_thread.run_sync` to
  the forkserver-worker thread. ✓ landed.
- Forked child: runs `_actor_child_main` against a normal
  trio runtime. ✓ landed.

Validated by `tests/spawn/test_subint_forkserver.py` (file
will be renamed to `test_main_thread_forkserver.py` in a
follow-up) including the
`test_subint_forkserver_spawn_basic` backend-tier check.

Still-open work (tracked on tractor #379):

- [ ] no cancellation / hard-kill stress coverage yet
  (counterpart to `tests/test_subint_cancellation.py` for
  the plain `subint` backend),

- [ ] `child_sigint='trio'` mode (flag scaffolded below; default
  is `'ipc'`). Originally intended as a manual SIGINT →
  trio-cancel bridge, but investigation showed trio's
  handler IS already correctly installed in the fork-child
  subactor — the orphan-SIGINT hang is actually a separate
  bug where trio's event loop stays wedged in `epoll_wait`
  despite delivery. See
  `ai/conc-anal/subint_forkserver_orphan_sigint_hang_issue.md`
  for the full trace + fix directions. Once that root cause
  is fixed, this flag may end up a no-op / doc-only mode.

TODO — cleanup gated on msgspec PEP 684 support
-----------------------------------------------

Both worker-thread primitives below allocate a dedicated
`threading.Thread` rather than using
`trio.to_thread.run_sync()`. That's a cautious design
rooted in three distinct-but-entangled issues (GIL
starvation from legacy-config subints, tstate-recycling
destroy race on trio cache threads, fork-from-main-tstate
invariant). Some of those dissolve under PEP 684
isolated-mode subints; one requires empirical re-testing
to know.

Full analysis + audit plan in
`ai/conc-anal/subint_forkserver_thread_constraints_on_pep684_issue.md`,
tracked at #450; gated on jcrist/msgspec#1026.

What lives here
---------------

Truly generic primitives (tractor-spawn-backend-agnostic):

- `_close_inherited_fds()`   — fd hygiene primitive
- `_format_child_exit()`     — `waitpid()` status renderer
- `wait_child()`             — synchronous waitpid wrapper
- `fork_from_worker_thread()` — the core fork primitive
- `_ForkedProc`              — trio-cancellable child-wait shim

The variant-1 spawn-backend coroutine on top:

- `main_thread_forkserver_proc()` — SpawnSpec handshake, IPC
  wiring, lifecycle. Registered as the
  `'main_thread_forkserver'` (and currently the legacy
  `'subint_forkserver'`-aliased) entry in
  `tractor.spawn._spawn._methods`.

See also
--------

- `tractor.spawn._subint_forkserver` — variant-2 placeholder
  module; reserved for the future subint-isolated-child
  runtime once jcrist/msgspec#1026 unblocks.

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
import errno
import os
import signal
import threading
from functools import partial
from typing import (
    Any,
    Callable,
    Literal,
    TYPE_CHECKING,
)

import trio
from trio import TaskStatus

from tractor.log import get_logger
from tractor.msg import (
    types as msgtypes,
    pretty_struct,
)
from tractor.runtime._state import current_actor
from tractor.runtime._portal import Portal
from ._spawn import (
    cancel_on_completion,
    soft_kill,
)

if TYPE_CHECKING:
    from tractor.discovery._addr import UnwrappedAddress
    from tractor.ipc import (
        _server,
    )
    from tractor.runtime._runtime import Actor
    from tractor.runtime._supervise import ActorNursery


log = get_logger('tractor')


# Configurable child-side SIGINT handling for forkserver-spawned
# subactors. Threaded through `main_thread_forkserver_proc`'s
# `proc_kwargs` under the `'child_sigint'` key.
#
# - `'ipc'` (default, currently the only implemented mode):
#   child has NO trio-level SIGINT handler — trio.run() is on
#   the fork-inherited non-main thread, `signal.set_wakeup_fd()`
#   is main-thread-only. Cancellation flows exclusively via
#   the parent's `Portal.cancel_actor()` IPC path. Safe +
#   deterministic for nursery-structured apps where the parent
#   is always the cancel authority. Known gap: orphan
#   (post-parent-SIGKILL) children don't respond to SIGINT
#   — see `test_orphaned_subactor_sigint_cleanup_DRAFT`.
#
# - `'trio'` (**not yet implemented**): install a manual
#   SIGINT → trio-cancel bridge in the child's fork prelude
#   (pre-`trio.run()`) so external Ctrl-C reaches stuck
#   grandchildren even with a dead parent. Adds signal-
#   handling surface the `'ipc'` default cleanly avoids; only
#   pay for it when externally-interruptible children actually
#   matter (e.g. CLI tool grandchildren).
ChildSigintMode = Literal['ipc', 'trio']
_DEFAULT_CHILD_SIGINT: ChildSigintMode = 'ipc'


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
        except OSError as oserr:
            # `EBADF` is the benign-and-expected case: the
            # `os.listdir('/proc/self/fd')` call above itself
            # opens a transient dirfd that ends up in
            # `candidates`, then auto-closes before this loop
            # reaches it. Same for any fd whose Python wrapper
            # was GC'd between `listdir` and `os.close`.
            # Suppress at debug-level — surfacing every
            # EBADF as a full traceback (prior `log.exception`
            # behavior) drowned the post-fork log channel.
            if oserr.errno == errno.EBADF:
                log.debug(
                    f'Skip already-closed inherited fd {fd!r} '
                    f'(EBADF, benign race with listdir)\n'
                )
                continue
            # Other errnos (EIO / EPERM / EINTR / ...) are
            # genuinely unexpected — keep the loud surface.
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

    def terminate(self) -> None:
        '''
        OS-level `SIGTERM` to the child. Swallows
        `ProcessLookupError` (already dead).

        Mirrors `trio.Process.terminate()` /
        `multiprocessing.Process.terminate()` — sends SIGTERM
        (graceful, allows the child a chance to clean up via
        signal-handlers) rather than SIGKILL. Used by
        `ActorNursery.cancel()`'s per-child escalation when
        `Portal.cancel_actor()` raises `ActorTooSlowError`,
        and by the legacy `hard_kill=True` branch on the same
        path.

        '''
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

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


async def main_thread_forkserver_proc(
    name: str,
    actor_nursery: ActorNursery,
    subactor: Actor,
    errors: dict[tuple[str, str], Exception],

    # passed through to actor main
    bind_addrs: list[UnwrappedAddress],
    parent_addr: UnwrappedAddress,
    _runtime_vars: dict[str, Any],
    *,
    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED,
    proc_kwargs: dict[str, any] = {},

) -> None:
    '''
    Spawn a subactor via `os.fork()` from a non-trio worker
    thread (see `fork_from_worker_thread()`), with the forked
    child running `tractor._child._actor_child_main()` and
    connecting back via tractor's normal IPC handshake.

    Supervision model mirrors `trio_proc()` — we manage a
    real OS subprocess, so `Portal.cancel_actor()` +
    `soft_kill()` on graceful teardown and `os.kill(SIGKILL)`
    on hard-reap both apply directly (no
    `_interpreters.destroy()` voodoo needed since the child
    is in its own process).

    The only real difference from `trio_proc` is the spawn
    mechanism: fork from a known-clean main-interp worker
    thread instead of `trio.lowlevel.open_process()`.

    '''
    # Backend-scoped config pulled from `proc_kwargs`. Using
    # `proc_kwargs` (vs a first-class kwarg on this function)
    # matches how other backends expose per-spawn tuning
    # (`trio_proc` threads it to `trio.lowlevel.open_process`,
    # etc.) and keeps `ActorNursery.start_actor(proc_kwargs=...)`
    # as the single ergonomic entry point.
    child_sigint: ChildSigintMode = proc_kwargs.get(
        'child_sigint',
        _DEFAULT_CHILD_SIGINT,
    )
    if child_sigint not in ('ipc', 'trio'):
        raise ValueError(
            f'Invalid `child_sigint={child_sigint!r}` for '
            f'`main_thread_forkserver` backend.\n'
            f'Expected one of: {ChildSigintMode}.'
        )
    if child_sigint == 'trio':
        raise NotImplementedError(
            "`child_sigint='trio'` mode — trio-native SIGINT "
            "plumbing in the fork-child — is scaffolded but "
            "not yet implemented. See the xfail'd "
            "`test_orphaned_subactor_sigint_cleanup_DRAFT` "
            "and the TODO in this module's docstring."
        )

    uid: tuple[str, str] = subactor.aid.uid
    loglevel: str | None = subactor.loglevel

    # Closure captured into the fork-child's memory image.
    # In the child this is the first post-fork Python code to
    # run, on what was the fork-worker thread in the parent.
    # `child_sigint` is captured here so the impl lands inside
    # this function once the `'trio'` mode is wired up —
    # nothing above this comment needs to change.
    def _child_target() -> int:
        # Dispatch on the captured SIGINT-mode closure var.
        # Today only `'ipc'` is reachable (the `'trio'` branch
        # is fenced off at the backend-entry guard above); the
        # match is in place so the future `'trio'` impl slots
        # in as a plain case arm without restructuring.
        match child_sigint:
            case 'ipc':
                pass  # <- current behavior: no child-side
                      #    SIGINT plumbing; rely on parent
                      #    `Portal.cancel_actor()` IPC path.
            case 'trio':
                # Unreachable today (see entry-guard above);
                # this stub exists so that lifting the guard
                # is the only change required to enable
                # `'trio'` mode once the SIGINT wakeup-fd
                # bridge is implemented.
                raise NotImplementedError(
                    "`child_sigint='trio'` fork-prelude "
                    "plumbing not yet wired."
                )
        # Lazy import so the parent doesn't pay for it on
        # every spawn — it's module-level in `_child` but
        # cheap enough to re-resolve here.
        from tractor._child import _actor_child_main
        # XXX, `os.fork()` inherits the parent's entire memory
        # image, including `tractor.runtime._state._runtime_vars`
        # (which in the parent encodes "this process IS the root
        # actor"). A fresh `exec`-based child starts cold; we
        # replicate that here by explicitly resetting runtime
        # vars to their fresh-process defaults — otherwise
        # `Actor.__init__` takes the `is_root_process() == True`
        # branch, pre-populates `self.enable_modules`, and trips
        # the `assert not self.enable_modules` gate at the top
        # of `Actor._from_parent()` on the subsequent parent→
        # child `SpawnSpec` handshake. (`_state._current_actor`
        # is unconditionally overwritten by `_trio_main` → no
        # reset needed for it.)
        from tractor.runtime._state import (
            get_runtime_vars,
            set_runtime_vars,
        )
        set_runtime_vars(get_runtime_vars(clear_values=True))
        _actor_child_main(
            uid=uid,
            loglevel=loglevel,
            parent_addr=parent_addr,
            infect_asyncio=infect_asyncio,
            # The child's runtime is trio-native (uses
            # `_trio_main` + receives `SpawnSpec` over IPC),
            # but label it with the actual parent-side spawn
            # mechanism so `Actor.pformat()` / log lines
            # reflect reality. Downstream runtime gates that
            # key on `_spawn_method` group `main_thread_forkserver`
            # alongside `trio`/`subint` where the SpawnSpec
            # IPC handshake is concerned — see
            # `runtime._runtime.Actor._from_parent()`.
            spawn_method='main_thread_forkserver',
        )
        return 0

    cancelled_during_spawn: bool = False
    proc: _ForkedProc | None = None
    ipc_server: _server.Server = actor_nursery._actor.ipc_server

    try:
        try:
            pid: int = await trio.to_thread.run_sync(
                partial(
                    fork_from_worker_thread,
                    _child_target,
                    thread_name=(
                        f'main-thread-forkserver[{name}]'
                    ),
                ),
                abandon_on_cancel=False,
            )
            proc = _ForkedProc(pid)
            log.runtime(
                f'Forked subactor via main-thread-forkserver\n'
                f'(>\n'
                f' |_{proc}\n'
            )

            event, chan = await ipc_server.wait_for_peer(uid)

        except trio.Cancelled:
            cancelled_during_spawn = True
            raise

        assert proc is not None

        portal = Portal(chan)
        actor_nursery._children[uid] = (
            subactor,
            proc,
            portal,
        )

        sspec = msgtypes.SpawnSpec(
            _parent_main_data=subactor._parent_main_data,
            enable_modules=subactor.enable_modules,
            reg_addrs=subactor.reg_addrs,
            bind_addrs=bind_addrs,
            _runtime_vars=_runtime_vars,
        )
        log.runtime(
            f'Sending spawn spec to forkserver child\n'
            f'{{}}=> {chan.aid.reprol()!r}\n'
            f'\n'
            f'{pretty_struct.pformat(sspec)}\n'
        )
        await chan.send(sspec)

        curr_actor: Actor = current_actor()
        curr_actor._actoruid2nursery[uid] = actor_nursery

        task_status.started(portal)

        with trio.CancelScope(shield=True):
            await actor_nursery._join_procs.wait()

        async with trio.open_nursery() as nursery:
            if portal in actor_nursery._cancel_after_result_on_exit:
                nursery.start_soon(
                    cancel_on_completion,
                    portal,
                    subactor,
                    errors,
                )

            # reuse `trio_proc`'s soft-kill dance — `proc`
            # is our `_ForkedProc` shim which implements the
            # same `.poll()` / `.wait()` / `.kill()` surface
            # `soft_kill` expects.
            await soft_kill(
                proc,
                _ForkedProc.wait,
                portal,
            )
            nursery.cancel_scope.cancel()

    finally:
        # Hard reap: SIGKILL + waitpid. Cheap since we have
        # the real OS pid, unlike `subint_proc` which has to
        # fuss with `_interpreters.destroy()` races.
        if proc is not None and proc.poll() is None:
            log.cancel(
                f'Hard killing main-thread-forkserver subactor\n'
                f'>x)\n'
                f' |_{proc}\n'
            )
            with trio.CancelScope(shield=True):
                proc.kill()
                await proc.wait()

        if not cancelled_during_spawn:
            actor_nursery._children.pop(uid, None)
