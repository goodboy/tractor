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
as last resort with a bounded grace window) plus optional
`/dev/shm/` orphan-segment sweep.

Shared implementation between the `tractor-reap` CLI
(`scripts/tractor-reap`) and the pytest session-scoped
auto-fixture that guards the test suite against leftover
subactor processes.

Design notes — process reap
---------------------------

- Linux-only today: reads `/proc/<pid>/{status,cwd,cmdline}`.
  Module imports cleanly elsewhere; calling `find_*` on a
  non-Linux box returns an empty list (no `/proc`
  enumeration). A future xplatform pass could swap this
  for `psutil.Process.children()` /
  `psutil.process_iter()` since `psutil` is already a
  test-time dependency.

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

Design notes — shm sweep
------------------------

Since `tractor/ipc/_mp_bs.disable_mantracker()` turns off
`mp.resource_tracker` entirely, a hard-crashing actor can
leave `/dev/shm/<key>` segments behind that nothing else
GCs (see
`ai/conc-anal/subint_forkserver_mp_shared_memory_issue.md`,
"Trade-offs / known gaps").

The shm sweep is **Linux-/FreeBSD-only**: both expose
POSIX shared-memory segments as regular files under
`/dev/shm`, so `os.stat()` + `os.unlink()` are the
correct primitives. macOS POSIX shm has no fs-visible
path (segments live behind `shm_open`/`shm_unlink`
syscalls only), and Windows is a different story
entirely. Calling the shm helpers on an unsupported
platform raises `NotImplementedError`.

In-use enumeration delegates to `psutil` —
`Process.memory_maps()` (post-mmap) +
`Process.open_files()` (pre-mmap shm-opened fds) —
xplatform, mature, and handles the per-process
permission/race edge cases correctly. Segments matching
neither are genuinely leaked → safe to unlink.

The "nobody has it open" check is the kernel-canonical
test — same answer `lsof /dev/shm/<key>` would give. No
reliance on tractor-specific naming conventions (shm
keys are caller-defined).

'''
from __future__ import annotations

import os
import pathlib
import signal
import stat
import sys
import time

# `/dev/shm` is the POSIX-shm filesystem on Linux + FreeBSD.
# macOS uses `shm_open` syscalls without a fs-visible path,
# so the shm helpers refuse to run there.
_SHM_PLATFORM_OK: bool = sys.platform.startswith(
    ('linux', 'freebsd')
)
SHM_DIR: str = '/dev/shm'


def _ensure_shm_supported() -> None:
    '''
    Guard for shm helpers — they assume `/dev/shm` exists
    as a tmpfs and `os.unlink()` is the right primitive.
    Both true on Linux + FreeBSD; not true elsewhere.

    '''
    if not _SHM_PLATFORM_OK:
        raise NotImplementedError(
            f'shm reap is only supported on Linux/FreeBSD; '
            f'got sys.platform={sys.platform!r}. macOS '
            f'POSIX shm has no fs-visible path; Windows '
            f'has no /dev/shm equivalent.'
        )


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
    except (
        FileNotFoundError,
        PermissionError,
        ProcessLookupError,
    ):
        return None
    return None


def _read_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except (
        FileNotFoundError,
        PermissionError,
        ProcessLookupError,
    ):
        return None


def _read_cmdline(pid: int) -> str:
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return f.read().replace(b'\0', b' ').decode(
                errors='replace',
            )
    except (
        FileNotFoundError,
        PermissionError,
        ProcessLookupError,
    ):
        return ''


def _iter_live_pids() -> list[int]:
    '''
    Enumerate currently-alive pids from `/proc`. Returns
    `[]` on systems without `/proc` (e.g. macOS).

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
    except (
        FileNotFoundError,
        ProcessLookupError,
    ):
        return False
    return True


def _enumerate_in_use_shm(
    shm_dir: str = SHM_DIR,
) -> set[str]:
    '''
    Return the set of `<shm_dir>/<file>` paths currently
    held open by any live process — via `psutil`'s
    xplatform `Process.memory_maps()` (post-mmap
    segments) and `Process.open_files()` (pre-mmap
    shm-opened fds).

    Lazy-imports `psutil` so the module stays importable
    on installs without it (it's a `testing` group dep).

    '''
    _ensure_shm_supported()

    # lazy + actionable failure: leaked shm sweep is the
    # only thing in this module that needs psutil; we
    # don't want a top-level ImportError breaking the
    # process-reap path.
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError(
            'shm reap requires `psutil` — install the '
            '`testing` dep group, e.g. '
            '`uv sync --group testing`.'
        ) from exc

    in_use: set[str] = set()
    prefix: str = shm_dir.rstrip('/') + '/'
    for proc in psutil.process_iter(['pid']):
        try:
            for m in proc.memory_maps(grouped=False):
                if m.path.startswith(prefix):
                    in_use.add(m.path)
            for f in proc.open_files():
                if f.path.startswith(prefix):
                    in_use.add(f.path)
        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
            FileNotFoundError,
            PermissionError,
        ):
            # raced — proc died or we can't see its
            # mappings (e.g. root-owned). Skip; missing
            # an in-use entry only means we'd preserve
            # something we could reap, never the
            # reverse — safe-by-default.
            continue
    return in_use


def find_orphaned_shm(
    *,
    uid: int | None = None,
    shm_dir: str = SHM_DIR,
) -> list[str]:
    '''
    `<shm_dir>/<file>` paths that are:

    - owned by `uid` (default: the current effective uid),
    - and currently held by NO live process — i.e.
      genuinely leaked.

    Linux/FreeBSD only — see module docstring. No reliance
    on caller-defined shm-key naming, so this works for
    any tractor app (not just the test suite).

    '''
    _ensure_shm_supported()

    if uid is None:
        uid = os.geteuid()

    try:
        entries: list[str] = os.listdir(shm_dir)
    except OSError:
        return []

    in_use: set[str] = _enumerate_in_use_shm(shm_dir=shm_dir)
    leaked: list[str] = []
    prefix: str = shm_dir.rstrip('/') + '/'
    for entry in entries:
        path: str = prefix + entry
        try:
            st: os.stat_result = os.stat(path)
        except OSError:
            continue
        # only regular files — skip subdirs / sockets etc.
        if not stat.S_ISREG(st.st_mode):
            continue
        if st.st_uid != uid:
            continue
        if path in in_use:
            continue
        leaked.append(path)
    return leaked


def reap_shm(
    paths: list[str],
    *,
    log=print,
) -> tuple[list[str], list[tuple[str, OSError]]]:
    '''
    Unlink the given `/dev/shm/...` paths.

    Linux/FreeBSD only — `os.unlink()` is the correct
    primitive on the POSIX-shm tmpfs there. macOS POSIX
    shm has no fs-visible path; the equivalent there is
    `posix_ipc.unlink_shared_memory(name)` (not
    implemented here — see module docstring).

    Returns `(unlinked, errors)` where `errors` is a list
    of `(path, exc)` for paths that could not be removed
    (e.g. permissions). Paths that raced to being already-
    gone are counted as successfully unlinked.

    '''
    _ensure_shm_supported()

    unlinked: list[str] = []
    errors: list[tuple[str, OSError]] = []
    for path in paths:
        try:
            os.unlink(path)
            unlinked.append(path)
        except FileNotFoundError:
            # raced — already gone, treat as success
            unlinked.append(path)
        except OSError as exc:
            errors.append((path, exc))

    if unlinked:
        log(
            f'[tractor-reap] unlinked {len(unlinked)} '
            f'orphaned shm segment(s): {unlinked}'
        )
    for path, exc in errors:
        log(
            f'[tractor-reap] could not unlink {path}: '
            f'{exc!r}'
        )
    return (unlinked, errors)
