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
Defensive monkey-patches for `trio` internals.

Every patch in this package fixes a bug in `trio` itself
that we've encountered while running `tractor` — usually
a fork-survival edge case that upstream `trio` hasn't
filed/fixed yet. Each patch is:

- **idempotent** — safe to call multiple times
- **version-gated** — checks `trio.__version__` and skips
  itself if upstream has shipped the fix
- **scoped** — only modifies the specific trio internal
  it's targeting; no broad side effects
- **removable** — every patch carries a `# REMOVE WHEN:`
  marker in its docstring pointing at the upstream PR
  whose release allows us to drop it

Add a new patch by:

1. Create `tractor/trionics/patches/_<topic>.py` exposing
   the `apply()` / `is_needed()` / `repro()` API
   contract.
2. Import it in this `__init__.py` and add an entry to
   `_PATCHES`.
3. Document upstream-fix-tracking in the module
   docstring's `# REMOVE WHEN:` line.
4. Add a regression test in
   `tests/trionics/test_patches.py` that uses the
   patch's `repro()` to assert the bug exists + the
   patch fixes it.

Calling `apply_all()` from a tractor entry point (e.g.
`tractor._child._actor_child_main`) applies every
registered patch + returns `{patch_name: applied?}` so
callers can log/assert as needed.

'''
from typing import Callable

from . import _wakeup_socketpair


_PATCHES: list[tuple[str, Callable[[], bool]]] = [
    (
        'trio_wakeup_socketpair_drain_eof',
        _wakeup_socketpair.apply,
    ),
]


def apply_all() -> dict[str, bool]:
    '''
    Apply every registered patch. Idempotent — calling
    twice is fine, second call's dict will be all
    `False`.

    Returns `{patch_name: applied?}`:

    - `True` — patch was applied THIS call (inaugural
      apply, or first-call-since-process-start).
    - `False` — skipped (already applied OR upstream fix
      detected via `is_needed() == False`).

    '''
    results: dict[str, bool] = {}
    for name, applier in _PATCHES:
        results[name] = applier()
    return results
