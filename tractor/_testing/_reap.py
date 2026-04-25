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
Zombie-subactor reaper — SC-polite (SIGINT first, SIGKILL
as last resort with a bounded grace window).

Shared implementation between the `tractor-reap` CLI
(`scripts/tractor-reap`) and the pytest session-scoped
auto-fixture that guards the test suite against leftover
subactor processes.

Design notes
------------

- Linux-only: reads `/proc/<pid>/{status,cwd,cmdline}`.
- Two detection modes:

  1. **descendant-mode** — when invoked from a still-live
     parent (e.g. a pytest session-end fixture), match by
     `PPid == parent_pid`. Direct + precise; the target
     PIDs are still reparented to the live pytest process
     at teardown time, before pytest exits.

  2. **orphan-mode** — when invoked after the parent died
     (e.g. the `tractor-reap` CLI run post-Ctrl+C), match
     by `PPid == 1` (reparented to init) AND `cwd ==
     <repo-root>` AND cmdline contains `python`. The cwd
     filter is what keeps the heuristic from sweeping up
     unrelated init-children on the box.

- Escalation: for every matched PID, SIGINT, poll for up
  to `grace` seconds, then SIGKILL any survivors. The
  two-phase pattern is the SC-graceful-cancel discipline
  documented in `feedback_sc_graceful_cancel_first.md` —
  we want the subactor runtime to run its trio cancel
  shield + IPC teardown paths where it can.

'''
from __future__ import annotations

import os
import pathlib
import signal
import time


def _read_status_ppid(pid: int) -> int | None:
    '''
    Return the parent-pid from `/proc/<pid>/status` or
    `None` if the proc went away / is unreadable.

    '''
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('PPid:'):
                    return int(line.split()[1])
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    return None


def _read_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None


def _read_cmdline(pid: int) -> str:
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return f.read().replace(b'\0', b' ').decode(errors='replace')
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return ''


def _iter_live_pids() -> list[int]:
    '''
    Enumerate currently-alive pids from `/proc`.

    '''
    try:
        entries: list[str] = os.listdir('/proc')
    except OSError:
        return []
    return [int(e) for e in entries if e.isdigit()]


def find_descendants(
    parent_pid: int,
) -> list[int]:
    '''
    PIDs whose `PPid == parent_pid` — i.e. direct
    children of the given pid. Used by the pytest
    session-end fixture where `parent_pid` is still
    alive as the pytest-python process.

    '''
    return [
        pid
        for pid in _iter_live_pids()
        if _read_status_ppid(pid) == parent_pid
    ]


def find_orphans(
    repo_root: pathlib.Path,
) -> list[int]:
    '''
    PIDs that are:

    - reparented to init (`PPid == 1`),
    - have `cwd == <repo_root>`,
    - and have a `python` in their cmdline.

    This is the "pytest-died-mid-session" case where the
    subactor forks got reparented. The cwd filter is the
    critical bit that keeps us from sweeping up unrelated
    init-children on the box.

    '''
    repo: str = str(repo_root)
    hits: list[int] = []
    for pid in _iter_live_pids():
        if _read_status_ppid(pid) != 1:
            continue
        cwd: str | None = _read_cwd(pid)
        if cwd != repo:
            continue
        cmd: str = _read_cmdline(pid)
        if 'python' not in cmd:
            continue
        hits.append(pid)
    return hits


def reap(
    pids: list[int],
    *,
    grace: float = 3.0,
    poll: float = 0.25,
    log=print,
) -> tuple[list[int], list[int]]:
    '''
    Deliver SIGINT to each pid, wait up to `grace`
    seconds for them to exit, then SIGKILL any that
    survive.

    Returns `(signalled, survivors_killed)` so callers
    can report / assert.

    `log` is the logger function for user-visible
    progress lines — default `print`; pytest fixture
    swaps it for a `pytest`-friendly writer.

    '''
    if not pids:
        return ([], [])

    signalled: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGINT)
            signalled.append(pid)
        except ProcessLookupError:
            # raced — already gone
            pass

    if signalled:
        log(
            f'[tractor-reap] SIGINT → {len(signalled)} '
            f'proc(s): {signalled}'
        )

    deadline: float = time.monotonic() + grace
    while time.monotonic() < deadline:
        time.sleep(poll)
        alive: list[int] = [
            pid for pid in signalled if _is_alive(pid)
        ]
        if not alive:
            return (signalled, [])

    survivors: list[int] = [
        pid for pid in signalled if _is_alive(pid)
    ]
    if survivors:
        log(
            f'[tractor-reap] SIGKILL (after {grace}s '
            f'grace) → {survivors}'
        )
        for pid in survivors:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    return (signalled, survivors)


def _is_alive(pid: int) -> bool:
    '''
    True iff `/proc/<pid>` still exists AND the proc
    isn't already a zombie (Z state).

    '''
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('State:'):
                    # e.g. 'State:\tZ (zombie)'
                    return 'Z' not in line.split()[1]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return True
