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
smoke-test and into tractor proper as the
`subint_forkserver` spawn backend.

Design rationale — why a forkserver, and why in-process
-------------------------------------------------------

There are two design questions worth pinning down up front,
since the name "subint_forkserver" intentionally evokes the
stdlib `multiprocessing.forkserver` for comparison:

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

The subint architecture lets us keep the forkserver
**in-process** because subints already provide the
state-isolation guarantee that `mp.forkserver`'s sidecar
buys via the process boundary. Concretely: in the envisioned
arch (currently partially landed — see "Status" below),

- the **main interpreter** stays trio-free and hosts the
  forkserver worker thread that owns `os.fork()`,
- the parent actor's **`trio.run()`** lives in a separate
  *sub-interpreter* (a different worker thread) — fully
  isolated `sys.modules` / `__main__` / globals from main,
- when a spawn is requested, the trio task signals the
  forkserver thread (intra-process, ~free) and the
  forkserver forks; the child inherits the parent's full
  in-memory state cheaply.

That collapses the three costs above:

- no sidecar — the forkserver is just another thread,
- spawn signal is a thread-local event/condition, not IPC,
- child inherits the warm parent state (loaded modules,
  populated caches, etc.) for free.

The tradeoff we accept in exchange: this design is
3.14-only (legacy-config subints still share the GIL, so
the parent's trio loop and the forkserver worker contend
on it; once PEP 684 isolated-mode + msgspec
[jcrist/msgspec#1026](https://github.com/jcrist/msgspec/issues/1026)
land, this constraint relaxes). And the dedicated worker
threads here are heavier than `trio.to_thread.run_sync`
calls — see the "TODO" section further down for the audit
plan once those upstream pieces land.

Future arch — what subints would buy us
---------------------------------------

The `subint` in this module's name is **family-naming
today** — currently the implementation only uses a regular
worker thread on the main interp; no subinterpreter is
created anywhere in the parent or child. The naming becomes
*literal* once jcrist/msgspec#1026 unblocks isolated-mode
subints (PEP 684 per-interp GIL). Three concrete wins land
at that point:

**(1) Cheaper forks (smaller main-interp COW image)**

Today the parent's main interp carries the full tractor
stack: trio runtime, msgspec codecs, IPC layer, every
user module the actor imported. When the forkserver
worker calls `os.fork()` the child inherits ALL of that
as COW memory — even though most gets overwritten when
the child boots its own `trio.run()`.

Move the parent's `trio.run()` into a subint (its own
`sys.modules` / `__main__` / globals) and the main
interp **stays minimal** — just the forkserver-thread
plumbing + bare CPython. The main interp becomes the
*literal* forkserver: an intentionally-empty execution
context whose only job is to call `os.fork()` cleanly.
Inherited COW image shrinks proportionally.

**(2) True parallelism between forkserver and trio
(per-interp GIL)**

Today the forkserver worker and the trio.run() thread
share the main GIL — when one runs the other waits.
Spawn requests briefly stall trio while the worker
takes the GIL to call `os.fork()`. PEP 684 isolated-
mode gives each subint its own GIL: forkserver thread
on main + trio on subint actually run in parallel.
Spawn latency drops, trio loop doesn't notice the
fork happening.

**(3) Multi-actor-per-process (the architectural prize)**

The bigger payoff and the reason `_subint.py` (the
in-thread `subint` backend) exists in parallel with
this module. With per-interp-GIL subints, one process
can host:

- main interp: forkserver thread + bookkeeping
- subint A: actor 1's `trio.run()`
- subint B: actor 2's `trio.run()`
- subint C: ...

`os.fork()` becomes the **last-resort** spawn — used
only when a new OS process is actually required
(cgroups, namespaces, security boundary, multi-host
distribution). Within a single process, subint-per-
actor is radically cheaper: no fork, no COW, no
inherited-fd cleanup — just `_interpreters.create()`
+ `_interpreters.exec()`.

The two backends converge on a coherent story:
`subint` → in-process spawn (cheap, GIL-isolated),
`subint_forkserver` → cross-process spawn (when you
truly need OS-level isolation). The forkserver isn't
the default mechanism; it's the bridge to a new
process when subint isolation isn't enough.

Implementation status — what's wired today
-----------------------------------------

The "envisioned arch" above is the eventual target; the
**currently-landed** flow is a partial step toward it:

- A dedicated main-interp worker thread owns all `os.fork()`
  calls (never enters a subint). ✓ landed.
- Parent actor's `trio.run()` lives **on the main interp**
  for now (not a subint yet). The subint-hosted root
  runtime is gated on jcrist/msgspec#1026 (see
  `_subint.py` docstring).
- Spawn-request signal: trio task `→ to_thread.run_sync`
  to the forkserver-worker thread. ✓ landed.
- Forked child: runs `_actor_child_main` against a normal
  trio runtime. ✓ landed.

The "subint" in the backend name refers to the *family* —
this backend ships in the same PR series as `_subint.py`
(in-thread subint backend) and `_subint_fork.py` (the RFC
stub for fork-from-non-main-subint, blocked upstream).
Once the parent's trio also lives in a subint we'll have
the full envisioned arch; until then the forkserver
half is independently useful and ship-able.

What survives the fork? — POSIX semantics
-----------------------------------------

A natural worry when forking from a parent that's running
`trio.run()` on another thread: does that trio thread (and
any other threads in the parent) keep running in the child?

**No.** POSIX `fork()` only preserves the *calling* thread
in the child. Every other thread in the parent — trio's
runner thread, any `to_thread` cache threads, anything else
— is gone the instant `fork()` returns in the child.

Concretely, after the forkserver worker calls `os.fork()`:

| thread                | parent    | child         |
|-----------------------|-----------|---------------|
| forkserver worker     | continues | sole survivor |
| `trio.run()` thread   | continues | gone          |
| any other thread      | continues | gone          |

The forkserver worker becomes the new "main" execution
context in the child; `trio.run()` and every other
parent thread never executes a single instruction
post-fork in the child.

This is exactly *why* `os.fork()` is delegated to a
dedicated worker thread that has provably never entered
trio: we want that trio-free thread to be the surviving
one in the child.

That said, dead-thread *artifacts* still cross the fork
boundary (canonical "fork in a multithreaded program is
dangerous" — see `man pthread_atfork`). What persists, and
how we handle each:

- **Inherited file descriptors** — the dead trio thread's
  epoll fd, signal-wakeup-fd, eventfds, sockets, IPC
  pipes, pytest's capture-fds, etc. are all still in the
  child's fd table (kernel-level inheritance). Handled by
  `_close_inherited_fds()` in the child prelude — walks
  `/proc/self/fd` and closes everything except stdio +
  the channel pipe to the forkserver.
- **Memory image** — trio's internal data structures
  (scheduler, task queues, runner state) sit in COW
  memory but nobody's executing them. Get GC'd /
  overwritten when the child's fresh `trio.run()` boots.
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
[jcrist/msgspec#1026](https://github.com/jcrist/msgspec/issues/1026)
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
import sys
import threading
from functools import partial
from typing import (
    Any,
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
# Lower-level fork primitives — see module docstring for the
# split rationale. `_subint_forkserver` builds tractor's
# subint-family spawn backend on top of these.
from ._main_thread_forkserver import (
    _close_inherited_fds as _close_inherited_fds,
    _format_child_exit as _format_child_exit,
    fork_from_worker_thread as fork_from_worker_thread,
    wait_child as wait_child,
    _ForkedProc,
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
            log.exception(
                f'Failed to .exec() in subint ??\n'
                f'_interpreters.exec(\n'
                f'    interp_id={interp_id!r},\n'
                f'    bootstrap={bootstrap!r},\n'
                f') => {err!r}\n'
            )

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
        # If stdout/stderr point at a PIPE (not a TTY or
        # regular file), we're almost certainly running under
        # pytest's default `--capture=fd` or some other
        # capturing harness. Under high-volume subactor error-
        # log output (e.g. the cancel cascade spew in nested
        # `run_in_actor` failures) the Linux 64KB pipe buffer
        # fills faster than the reader drains → child `write()`
        # blocks → child can't finish teardown → parent's
        # `_ForkedProc.wait` blocks → cascade deadlock.
        # Sever inheritance by redirecting fds 1,2 to
        # `/dev/null` in that specific case. TTY/file stdio
        # is preserved so interactive runs still see subactor
        # output. See `.claude/skills/run-tests/SKILL.md`
        # section 9 and
        # `ai/conc-anal/subint_forkserver_test_cancellation_leak_issue.md`
        # for the post-mortem.
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
            # key on `_spawn_method` group `subint_forkserver`
            # alongside `trio`/`subint` where the SpawnSpec
            # IPC handshake is concerned — see
            # `runtime._runtime.Actor._from_parent()`.
            spawn_method='subint_forkserver',
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
