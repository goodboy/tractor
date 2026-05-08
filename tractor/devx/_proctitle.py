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
Per-actor proc-title via `py-setproctitle`.

Sets a stable, OS-level identifier for each `tractor` actor
process so diag tools (`ps`, `top`, `htop`, `psutil`) and our
own `acli.pytree`/`acli.hung_dump` can show "which actor is
which" at a glance without needing to read full
`/proc/<pid>/cmdline`.

Format:
  ``tractor[<aid.reprol()>]``    e.g. ``tractor[doggy@1027301b]``

Uses the canonical `Aid.reprol()` form
(``<name>@<uuid_short>``) so the proc-title matches the
identifier shape used in tractor's logs, the `TRACTOR_AID`
env-var, and orphan-reaper scans — one identity across
all surfaces.

Optional dep: silently no-op when `setproctitle` is missing.

'''
from __future__ import annotations
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from tractor.runtime._runtime import Actor


# `setproctitle` is an optional dep — tractor's runtime path
# treats this as best-effort diag, so missing import is a
# no-op rather than a hard error.
try:
    import setproctitle as _stp
except ImportError:
    _stp = None


def set_actor_proctitle(actor: 'Actor') -> str | None:
    '''
    Set the calling process's proc-title to identify it as a
    tractor sub-actor.

    Returns the title string set, or `None` if `setproctitle`
    isn't available.

    Should be called early in the actor's process lifetime
    (after `Actor` construction, before `_trio_main`) so the
    new title is visible to OS-level tooling for the entire
    runtime.

    '''
    if _stp is None:
        return None

    title: str = f'tractor[{actor.aid.reprol()}]'
    _stp.setproctitle(title)
    return title
