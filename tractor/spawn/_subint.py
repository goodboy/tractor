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
Sub-interpreter (`subint`) actor spawning backend.

Spawns each sub-actor as a CPython PEP 734 sub-interpreter
(`concurrent.interpreters.Interpreter`) — same-process state
isolation with faster start-up than an OS subproc, while
preserving tractor's IPC-based actor boundaries.

Availability
------------
Requires Python 3.14+ for the stdlib `concurrent.interpreters`
module. On older runtimes the module still imports (so the
registry stays introspectable) but `subint_proc()` raises.

Status
------
SCAFFOLDING STUB — `subint_proc()` is **not yet implemented**.
The real impl lands in Phase B.2 (see issue #379).

'''
from __future__ import annotations
import sys
from typing import (
    Any,
    TYPE_CHECKING,
)

import trio
from trio import TaskStatus


try:
    from concurrent import interpreters as _interpreters  # type: ignore
    _has_subints: bool = True
except ImportError:
    _interpreters = None  # type: ignore
    _has_subints: bool = False


if TYPE_CHECKING:
    from tractor.discovery._addr import UnwrappedAddress
    from tractor.runtime._portal import Portal
    from tractor.runtime._runtime import Actor
    from tractor.runtime._supervise import ActorNursery


async def subint_proc(
    name: str,
    actor_nursery: ActorNursery,
    subactor: Actor,
    errors: dict[tuple[str, str], Exception],

    # passed through to actor main
    bind_addrs: list[UnwrappedAddress],
    parent_addr: UnwrappedAddress,
    _runtime_vars: dict[str, Any],  # serialized and sent to _child
    *,
    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED,
    proc_kwargs: dict[str, any] = {}

) -> None:
    '''
    Create a new sub-actor hosted inside a PEP 734
    sub-interpreter running in a dedicated OS thread,
    reusing tractor's existing UDS/TCP IPC handshake
    for parent<->child channel setup.

    NOT YET IMPLEMENTED — placeholder stub pending the
    Phase B.2 impl.

    '''
    if not _has_subints:
        raise RuntimeError(
            f'The {"subint"!r} spawn backend requires Python 3.14+ '
            f'(stdlib `concurrent.interpreters`, PEP 734).\n'
            f'Current runtime: {sys.version}'
        )

    raise NotImplementedError(
        'The `subint` spawn backend scaffolding is in place but '
        'the spawn-flow itself is not yet implemented.\n'
        'Tracking: https://github.com/goodboy/tractor/issues/379'
    )
