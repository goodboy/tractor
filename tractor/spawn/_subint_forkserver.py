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
Variant-2 (future) "subint forkserver" placeholder — reserved
for the eventual subint-isolated-child runtime variant.

> **Status:** placeholder. Today
> `--spawn-backend=subint_forkserver` aliases to
> `main_thread_forkserver_proc` (variant 1, see
> `tractor.spawn._main_thread_forkserver`). A follow-up commit
> in this PR series flips the alias to a `NotImplementedError`
> stub reserving the `'subint_forkserver'` key for the literal
> subint-hosted-child variant once
> [jcrist/msgspec#1026](https://github.com/jcrist/msgspec/issues/1026)
> unblocks PEP 684 isolated-mode subints upstream.

Future arch — what subints would buy us
---------------------------------------

When msgspec#1026 unblocks isolated-mode subints (PEP 684
per-interp GIL), three concrete wins land — these are the
reason the `'subint_forkserver'` key is reserved as a
distinct backend rather than just folded into
`'main_thread_forkserver'`:

**(1) Cheaper forks (smaller main-interp COW image)**

Today (variant 1) the parent's main interp carries the full
tractor stack: trio runtime, msgspec codecs, IPC layer,
every user module the actor imported. When the forkserver
worker calls `os.fork()` the child inherits ALL of that as
COW memory — even though most gets overwritten when the
child boots its own `trio.run()`.

Variant 2 moves the parent's `trio.run()` into a subint (its
own `sys.modules` / `__main__` / globals). The main interp
**stays minimal** — just the forkserver-thread plumbing +
bare CPython. The main interp becomes the *literal*
forkserver: an intentionally-empty execution context whose
only job is to call `os.fork()` cleanly. Inherited COW image
shrinks proportionally.

**(2) True parallelism between forkserver and trio
(per-interp GIL)**

Variant-1 today: the forkserver worker and the trio.run()
thread share the main GIL — when one runs the other waits.
Spawn requests briefly stall trio while the worker takes
the GIL to call `os.fork()`. PEP 684 isolated-mode gives
each subint its own GIL: forkserver thread on main + trio
on subint actually run in parallel. Spawn latency drops,
trio loop doesn't notice the fork happening.

**(3) Multi-actor-per-process (the architectural prize)**

The bigger payoff and the reason `_subint.py` (the in-thread
`subint` backend) exists in parallel with this module. With
per-interp-GIL subints, one process can host:

- main interp: forkserver thread + bookkeeping
- subint A: actor 1's `trio.run()`
- subint B: actor 2's `trio.run()`
- subint C: ...

`os.fork()` becomes the **last-resort** spawn — used only
when a new OS process is actually required (cgroups,
namespaces, security boundary, multi-host distribution).
Within a single process, subint-per-actor is radically
cheaper: no fork, no COW, no inherited-fd cleanup — just
`_interpreters.create()` + `_interpreters.exec()`.

The three backends converge on a coherent story:

- `subint` → in-process spawn (cheap, GIL-isolated),
- `main_thread_forkserver` → cross-process spawn today
  (variant 1, working),
- `subint_forkserver` → cross-process spawn with
  isolated-subint child (variant 2, this module, future).

What lives here today
---------------------

- `run_subint_in_worker_thread()` — companion primitive to
  `_main_thread_forkserver.fork_from_worker_thread()`. Creates
  a fresh `legacy`-config sub-interpreter and drives a given
  bootstrap code string through `_interpreters.exec()` on a
  dedicated worker thread; destroys the subint after the
  thread joins. Used today by the
  `subint_fork_from_main_thread_smoketest.py` feasibility
  check; will be wired into the variant-2
  `subint_forkserver_proc` spawn-coroutine when it lands.
- (legacy re-exports of fork primitives kept for backward-
  compatible imports until external consumers migrate to
  `_main_thread_forkserver`)

What will live here when variant 2 ships
----------------------------------------

- `subint_forkserver_proc()` — the variant-2 spawn-backend
  coroutine. Same fork machinery as variant 1, but the
  fork-child enters a fresh subint (via
  `run_subint_in_worker_thread`) before booting its
  `trio.run()`. Net effect: child runtime is GIL-isolated
  from the parent + any sibling actors in the same process.
- A stub `subint_forkserver_proc` is added in a follow-up
  commit that raises `NotImplementedError(...)` pointing at
  this docstring + jcrist/msgspec#1026 + tractor #379, so
  `--spawn-backend=subint_forkserver` errors cleanly today
  rather than silently aliasing variant 1.

See also
--------

- `tractor.spawn._main_thread_forkserver` — variant 1,
  working today; for the full design rationale, fork-
  semantics analysis, and trio×fork hazard breakdown.
- `tractor.spawn._subint` — the in-thread `subint` backend
  (one process, one actor per subint, no fork).
- `tractor.spawn._subint_fork` — RFC stub for the
  fork-from-non-main-subint strategy that is blocked at the
  CPython level.
- [#379](https://github.com/goodboy/tractor/issues/379)
  — subint backend umbrella tracking issue.
- [jcrist/msgspec#1026](https://github.com/jcrist/msgspec/issues/1026)
  — upstream blocker for PEP 684 isolated-mode subints.
- [#450](https://github.com/goodboy/tractor/issues/450) —
  thread-constraints audit follow-up tied to msgspec#1026.

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
