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
Patch `trio._core._wakeup_socketpair.WakeupSocketpair.drain()`
to break on peer-closed EOF.

Problem
-------
`drain()` loops on `self.wakeup_sock.recv(2**16)` and
exits ONLY on `BlockingIOError` (buffer-empty on a
non-blocking socket), NEVER on `recv() == b''`
(peer-closed FIN). When the socketpair's write-end
has been closed, `recv` returns 0 bytes each call →
infinite C-level tight loop → 100% CPU, no Python
checkpoints, no signal delivery, no progress.

Most reliably triggered under fork-spawn backends —
`os.fork()` + `_close_inherited_fds()` can leave a
`WakeupSocketpair` instance whose `write_sock` was
closed in the child (or whose peer-end is held by a
process that has since exited).

Repro
-----
```python
from trio._core._wakeup_socketpair import WakeupSocketpair
ws = WakeupSocketpair()
ws.write_sock.close()
ws.drain()  # spins forever pre-patch
```

Fix
---
One line: break the drain loop on `b''` EOF
in addition to the existing `BlockingIOError` exit.

```python
def _safe_drain(self) -> None:
    try:
        while True:
            data = self.wakeup_sock.recv(2**16)
            if not data:  # ← peer-closed; nothing more to drain
                return
    except BlockingIOError:
        pass
```

Upstream
--------
TODO: file at `python-trio/trio` — the standalone
`repro()` below + this docstring is the issue body's
evidence section.

REMOVE WHEN: trio>=`<TBD>` ships the EOF-break in
`_wakeup_socketpair.WakeupSocketpair.drain()`.

See also
--------
- `ai/conc-anal/trio_wakeup_socketpair_busy_loop_under_fork_issue.md`
- `ai/conc-anal/infected_asyncio_under_main_thread_forkserver_hang_issue.md`
  — sibling-bug analysis fixed by the same patch.

'''
from __future__ import annotations


# Module-local sentinel — set True by `apply()` after the
# first successful patch. Idempotency guard.
_APPLIED: bool = False


def is_needed() -> bool:
    '''
    True iff upstream `trio` is the broken version that
    needs our patch.

    Today: always True since no released `trio` has the
    fix. When upstream lands it, gate on:

    ```python
    from packaging.version import Version
    import trio
    return Version(trio.__version__) < Version('<TBD>')
    ```

    '''
    # TODO version-gate once upstream lands the fix.
    return True


def repro() -> None:
    '''
    Minimal hang demonstrator + regression test target.

    Returns CLEANLY when `apply()` has been called
    earlier in this process (the patched
    `_safe_drain` breaks on EOF). Spins forever
    UNPATCHED — caller should wrap with a wall-clock
    cap (e.g. `signal.alarm(N)` or `trio.fail_after`)
    to avoid hanging the test runner if regressing.

    Used by `tests/trionics/test_patches.py` to assert
    both:

    1. The bug exists upstream (sanity check the
       repro is real).
    2. Our patch fixes it (post-`apply()` returns
       cleanly).

    '''
    from trio._core._wakeup_socketpair import (
        WakeupSocketpair,
    )
    ws = WakeupSocketpair()
    ws.write_sock.close()
    ws.drain()  # ← targeted operation


def apply() -> bool:
    '''
    Apply the EOF-break patch to
    `WakeupSocketpair.drain`. Idempotent + version-
    gated.

    Returns:

    - `True` if patched THIS call (inaugural apply).
    - `False` if skipped (already applied this process,
      OR `is_needed() == False` because upstream fixed
      it).

    '''
    global _APPLIED
    if _APPLIED or not is_needed():
        return False

    from trio._core._wakeup_socketpair import (
        WakeupSocketpair as _WSP,
    )

    def _safe_drain(self) -> None:
        try:
            while True:
                data = self.wakeup_sock.recv(2**16)
                # XXX patch — break on EOF instead of
                # spinning. Upstream trio's `drain()`
                # only handles the `BlockingIOError`
                # (buffer-empty) case; missed the
                # peer-closed (`recv == b''`) case.
                if not data:
                    return
        except BlockingIOError:
            pass

    _WSP.drain = _safe_drain
    _APPLIED = True
    return True
