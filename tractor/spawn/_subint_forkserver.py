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
**EXPERIMENTAL** — wired as the `'subint_forkserver'` entry
in `tractor.spawn._spawn._methods` and selectable via
`try_set_start_method('subint_forkserver')` / `--spawn-backend
=subint_forkserver`. Parent-side spawn, child-side runtime
bring-up and normal portal-RPC teardown are validated by the
backend-tier test in
`tests/spawn/test_subint_forkserver.py::test_subint_forkserver_spawn_basic`.

Still-open work (tracked on tractor #379):

- no cancellation / hard-kill stress coverage yet (counterpart
  to `tests/test_subint_cancellation.py` for the plain
  `subint` backend),
- `child_sigint='trio'` mode (flag scaffolded below; default
  is `'ipc'`). Originally intended as a manual SIGINT →
  trio-cancel bridge, but investigation showed trio's handler
  IS already correctly installed in the fork-child subactor
  — the orphan-SIGINT hang is actually a separate bug where
  trio's event loop stays wedged in `epoll_wait` despite
  delivery. See
  `ai/conc-anal/subint_forkserver_orphan_sigint_hang_issue.md`
  for the full trace + fix directions. Once that root cause
  is fixed, this flag may end up a no-op / doc-only mode.
- child-side "subint-hosted root runtime" mode (the second
  half of the envisioned arch — currently the forked child
  runs plain `_trio_main` via `spawn_method='trio'`; the
  subint-hosted variant is still the future step gated on
  msgspec PEP 684 support),
- thread-hygiene audit of the two `threading.Thread`
  primitives below, gated on the same msgspec unblock
  (see TODO section further down).

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
import sys
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
# subactors. Threaded through `subint_forkserver_proc`'s
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
        Async blocking wait for the child's exit, off-loaded
        to a trio cache thread so we don't block the event
        loop on `waitpid()`. Safe to call multiple times;
        subsequent calls return the cached rc without
        re-issuing the syscall.

        '''
        if self._returncode is not None:
            return self._returncode
        _, status = await trio.to_thread.run_sync(
            os.waitpid,
            self.pid,
            0,
            abandon_on_cancel=False,
        )
        self._returncode = self._parse_status(status)
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


async def subint_forkserver_proc(
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
    if not _has_subints:
        raise RuntimeError(
            f'The {"subint_forkserver"!r} spawn backend '
            f'requires Python 3.14+.\n'
            f'Current runtime: {sys.version}'
        )

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
            f'`subint_forkserver` backend.\n'
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
            # NOTE, from the child-side runtime's POV it's
            # a regular trio actor — it uses `_trio_main`,
            # receives `SpawnSpec` over IPC, etc. The
            # `subint_forkserver` name is a property of HOW
            # the parent spawned, not of what the child is.
            spawn_method='trio',
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
                        f'subint-forkserver[{name}]'
                    ),
                ),
                abandon_on_cancel=False,
            )
            proc = _ForkedProc(pid)
            log.runtime(
                f'Forked subactor via forkserver\n'
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
                f'Hard killing forkserver subactor\n'
                f'>x)\n'
                f' |_{proc}\n'
            )
            with trio.CancelScope(shield=True):
                proc.kill()
                await proc.wait()

        if not cancelled_during_spawn:
            actor_nursery._children.pop(uid, None)
