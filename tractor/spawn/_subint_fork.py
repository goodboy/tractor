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
`subint_fork` spawn backend — BLOCKED at CPython level.

The idea was to use a sub-interpreter purely as a launchpad
from which to call `os.fork()`, sidestepping the well-known
trio+fork issues (python-trio/trio#1614 etc.) by guaranteeing
the forking interp had never imported `trio`.

**IT DOES NOT WORK ON CURRENT CPYTHON.** The fork syscall
itself succeeds (in the parent), but the forked CHILD
process aborts immediately during CPython's post-fork
cleanup — `PyOS_AfterFork_Child()` calls
`_PyInterpreterState_DeleteExceptMain()` which refuses to
operate when the current tstate belongs to a non-main
sub-interpreter.

Full annotated walkthrough from the user-visible error
(`Fatal Python error: _PyInterpreterState_DeleteExceptMain:
not main interpreter`) down to the specific CPython source
lines that enforce this is in
`ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md`.

We keep this submodule as a dedicated documentation of the
attempt. If CPython ever lifts the restriction (e.g., via a
force-destroy primitive or a hook that swaps tstate to main
pre-fork), the structural sketch preserved in this file's
git history is a concrete starting point for a working impl.

See also: issue #379's "Our own thoughts, ideas for
`fork()`-workaround/hacks..." section.

'''
from __future__ import annotations
import sys
from typing import (
    Any,
    TYPE_CHECKING,
)

import trio
from trio import TaskStatus

from tractor.runtime._portal import Portal
from ._subint import _has_subints


if TYPE_CHECKING:
    from tractor.discovery._addr import UnwrappedAddress
    from tractor.runtime._runtime import Actor
    from tractor.runtime._supervise import ActorNursery


async def subint_fork_proc(
    name: str,
    actor_nursery: ActorNursery,
    subactor: Actor,
    errors: dict[tuple[str, str], Exception],

    bind_addrs: list[UnwrappedAddress],
    parent_addr: UnwrappedAddress,
    _runtime_vars: dict[str, Any],
    *,
    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED,
    proc_kwargs: dict[str, any] = {},

) -> None:
    '''
    EXPERIMENTAL — currently blocked by a CPython invariant.

    Attempted design
    ----------------
    1. Parent creates a fresh legacy-config subint.
    2. A worker OS-thread drives the subint through a
       bootstrap that calls `os.fork()`.
    3. In the forked CHILD, `os.execv()` back into
       `python -m tractor._child` (fresh process).
    4. In the fork-PARENT, the launchpad subint is destroyed;
       parent-side trio task proceeds identically to
       `trio_proc()` (wait for child connect-back, send
       `SpawnSpec`, yield `Portal`, etc.).

    Why it doesn't work
    -------------------
    CPython's `PyOS_AfterFork_Child()` (in
    `Modules/posixmodule.c`) calls
    `_PyInterpreterState_DeleteExceptMain()` (in
    `Python/pystate.c`) as part of post-fork cleanup. That
    function requires the current `PyThreadState` belong to
    the **main** interpreter. When `os.fork()` is called
    from within a sub-interpreter, the child wakes up with
    its tstate still pointing at the (now-stale) subint, and
    this check fails with `PyStatus_ERR("not main
    interpreter")`, triggering a `fatal_error` goto and
    aborting the child process.

    CPython devs acknowledge the fragility with a
    `// Ideally we could guarantee tstate is running main.`
    comment right above the call site.

    See
    `ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md`
    for the full annotated walkthrough + upstream-report
    draft.

    Why we keep this stub
    ---------------------
    - Documents the attempt in-tree so the next person who
      has this idea finds the reason it doesn't work rather
      than rediscovering the same CPython-level dead end.
    - If CPython ever lifts the restriction (e.g., via a
      force-destroy primitive or a hook that swaps tstate
      to main pre-fork), this submodule's git history holds
      the structural sketch of what a working impl would
      look like.

    '''
    if not _has_subints:
        raise RuntimeError(
            f'The {"subint_fork"!r} spawn backend requires '
            f'Python 3.14+.\n'
            f'Current runtime: {sys.version}'
        )

    raise NotImplementedError(
        'The `subint_fork` spawn backend is blocked at the '
        'CPython level — `os.fork()` from a non-main '
        'sub-interpreter is refused by '
        '`PyOS_AfterFork_Child()` → '
        '`_PyInterpreterState_DeleteExceptMain()`, which '
        'aborts the child with '
        '`Fatal Python error: not main interpreter`.\n'
        '\n'
        'See '
        '`ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md` '
        'for the full analysis + upstream-report draft.'
    )
