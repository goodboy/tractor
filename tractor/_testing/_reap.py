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
import re
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

# UDS-socket leak sweep — see `find_orphaned_uds()` /
# `reap_uds()` below. Tractor's UDS transport
# (`tractor.ipc._uds`) creates sock files under
# `${XDG_RUNTIME_DIR}/tractor/<name>@<pid>.sock`; a
# crash / SIGKILL / mid-cancel teardown can leave the
# file behind because `os.unlink()` lives in the
# `_serve_ipc_eps` `finally:` block which doesn't always
# get to run on hard exits. The reaper here is best-effort
# cleanup for the test harness + the `tractor-reap` CLI.
_UDS_SUBDIR: str = 'tractor'
# `<actor-name>@<pid>.sock` — pid is the binder's pid at
# creation time. Special sentinel: `registry@1616.sock`
# uses the magic `1616` not a real pid (the root
# registrar's known address; see `UDSAddress.get_root`).
_UDS_NAME_RE: re.Pattern = re.compile(
    r'^(?P<name>.+)@(?P<pid>\d+)\.sock$'
)
_UDS_REGISTRY_SENTINEL_PID: int = 1616


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


def find_runaway_subactors(
    parent_pid: int,
    *,
    cpu_threshold: float = 95.0,
    sample_interval: float = 0.5,
    only_pids: set[int]|None = None,
) -> list[tuple[int, float, str]]:
    '''
    Return `(pid, cpu_pct, cmdline)` for any descendant
    of `parent_pid` currently burning CPU above
    `cpu_threshold` (default 95%) — the smoking-gun
    signature of a runaway tight-loop bug (e.g. a C-level
    `recvfrom` loop on a closed socket that missed EOF
    detection — see
    `ai/conc-anal/trio_wakeup_socketpair_busy_loop_under_fork_issue.md`).

    `cpu_percent(interval=sample_interval)` is the
    canonical psutil API for a "what %CPU is this proc
    using NOW" answer — it samples twice with a delta to
    compute true utilization.

    `only_pids` filters to a specific pre-snapshotted set
    (e.g. "pids spawned during this test only"); when
    `None`, all live descendants are checked.

    Returns `[]` when `psutil` isn't installed or no
    descendants match.

    '''
    try:
        import psutil
    except ImportError:
        return []

    candidates: list[int] = find_descendants(parent_pid)
    if only_pids is not None:
        candidates = [p for p in candidates if p in only_pids]
    if not candidates:
        return []

    runaways: list[tuple[int, float, str]] = []
    for pid in candidates:
        try:
            proc = psutil.Process(pid)
            cpu: float = proc.cpu_percent(
                interval=sample_interval,
            )
            if cpu < cpu_threshold:
                continue
            cmdline: str = ' '.join(proc.cmdline())
            runaways.append((pid, cpu, cmdline))
        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
        ):
            continue
    return runaways


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


def get_uds_dir() -> str|None:
    '''
    Path of tractor's per-user UDS sock-file dir
    (`${XDG_RUNTIME_DIR}/tractor/`).

    Returns `None` when `XDG_RUNTIME_DIR` is unset (e.g.
    non-systemd hosts, or inside a container without the
    var plumbed through). Caller should treat that as
    "no UDS leaks possible to detect — skip".

    '''
    xdg: str|None = os.environ.get('XDG_RUNTIME_DIR')
    if not xdg:
        return None
    return os.path.join(xdg, _UDS_SUBDIR)


def _parse_uds_name(filename: str) -> tuple[str, int]|None:
    '''
    Extract `(actor_name, pid)` from a tractor UDS sock
    filename. Returns `None` for unrecognized names.

    '''
    m = _UDS_NAME_RE.match(filename)
    if not m:
        return None
    return (m['name'], int(m['pid']))


def find_orphaned_uds(
    *,
    uds_dir: str|None = None,
) -> list[str]:
    '''
    `<uds_dir>/*.sock` paths whose binder pid is no
    longer alive (orphaned). Includes the
    `registry@1616.sock` sentinel — `1616` is a magic
    sentinel pid (not a real one) so the file's
    presence alone signals a leak from a dead session.

    Returns `[]` on platforms without `XDG_RUNTIME_DIR`
    or when the dir doesn't exist. Files whose name
    doesn't match the `<name>@<pid>.sock` pattern are
    skipped (we don't unlink things we don't recognize).

    '''
    dir_path: str = uds_dir or get_uds_dir()
    if not dir_path:
        return []

    try:
        entries: list[str] = os.listdir(dir_path)
    except OSError:
        return []

    leaked: list[str] = []
    prefix: str = dir_path.rstrip('/') + '/'
    for entry in entries:
        path: str = prefix + entry
        if not entry.endswith('.sock'):
            continue
        try:
            st: os.stat_result = os.stat(path)
        except OSError:
            continue
        # only sockets; skip stray regular files / subdirs
        if not stat.S_ISSOCK(st.st_mode):
            continue
        parsed = _parse_uds_name(entry)
        if parsed is None:
            # unknown naming — skip rather than risk
            # unlinking something we don't own
            continue
        _name, pid = parsed
        if pid == _UDS_REGISTRY_SENTINEL_PID:
            # sentinel — never a real pid; if the file
            # exists nobody live is "owning" it via
            # /proc lookup, so always orphaned
            leaked.append(path)
            continue
        if not _is_alive(pid):
            leaked.append(path)
    return leaked


def reap_uds(
    paths: list[str],
    *,
    log=print,
) -> tuple[list[str], list[tuple[str, OSError]]]:
    '''
    Unlink the given UDS sock-file paths.

    Returns `(unlinked, errors)`; race-already-gone
    `FileNotFoundError`s count as success. Same shape
    as `reap_shm` so callers can pipeline both.

    '''
    unlinked: list[str] = []
    errors: list[tuple[str, OSError]] = []
    for path in paths:
        try:
            os.unlink(path)
            unlinked.append(path)
        except FileNotFoundError:
            unlinked.append(path)
        except OSError as exc:
            errors.append((path, exc))

    if unlinked:
        log(
            f'[tractor-reap] unlinked {len(unlinked)} '
            f'orphaned UDS sock-file(s): {unlinked}'
        )
    for path, exc in errors:
        log(
            f'[tractor-reap] could not unlink {path}: '
            f'{exc!r}'
        )
    return (unlinked, errors)


# ----------------------------------------------------------
# Pytest fixtures — sub-plugin surface
# ----------------------------------------------------------
# Loaded as a pytest plugin via the `pytest_plugins` line in
# `tractor._testing.pytest`. Keeps the reaping infra (helpers
# above + fixtures below) co-located so adding a new reap
# target is a single-file change. Sibling-module
# (`tractor._testing.pytest`) keeps its core
# tractor-tooling surface (option/marker/parametrize hooks,
# `tractor_test` deco, transport / spawn-method fixtures)
# uncluttered.
import pytest


@pytest.fixture(
    scope='session',
    autouse=True,
)
def _reap_orphaned_subactors():
    '''
    Session-scoped autouse fixture: after the whole test
    session finishes, SIGINT any subactor processes still
    parented to this `pytest` process, wait a bounded
    grace window, then SIGKILL survivors.

    Rationale: under fork-based spawn backends (notably
    `main_thread_forkserver`), a test that times out or bails
    mid-teardown can leave subactor forks alive. Without
    this reap, they linger across sessions and compete
    for ports / inherit pytest's capture-pipe fds — which
    flakifies later tests. SC-polite discipline: SIGINT
    first to let the subactor's trio cancel shield + IPC
    teardown paths run before we escalate.

    Matching companion CLI: `scripts/tractor-reap` for
    the pytest-died-mid-session case.

    '''
    parent_pid: int = os.getpid()
    yield
    pids: list[int] = find_descendants(parent_pid)
    if pids:
        reap(pids, grace=3.0)
    # NOTE, sweep UDS sock-files AFTER reaping subactors —
    # killed actors' bind paths only become "orphaned" once
    # their owning pid is gone. See `find_orphaned_uds()`
    # for the leak-detection algorithm + the `1616`
    # registry-sentinel special case.
    leaked_uds: list[str] = find_orphaned_uds()
    if leaked_uds:
        reap_uds(leaked_uds)


@pytest.fixture(
    scope='function',
    autouse=True,
)
def _track_orphaned_uds_per_test():
    '''
    Per-test (function-scoped) autouse UDS sock-file leak
    detector + reaper.

    Snapshots `${XDG_RUNTIME_DIR}/tractor/` before and
    after each test; any `<name>@<pid>.sock` files
    created during the test that survive teardown AND
    whose creator pid is dead are surfaced as a loud
    warning AND reaped, so the next test starts with a
    clean dir.

    Why per-test (not just session-scoped): under
    `--tpt-proto=uds`, a single hard-killed subactor
    leaves a sock file that a sibling test's
    `wait_for_actor`/`find_actor` discovery probes can
    accidentally hit (FileExistsError on rebind, or
    epoll register on a half-closed peer-FIN'd fd → see
    issue #454). Catching the leak the test that caused
    it (vs. blanket session-end sweep) makes blame
    obvious + prevents cascade flakiness.

    Cheap: 2x `os.listdir` + a few `os.stat`s per test.
    Skips silently when `XDG_RUNTIME_DIR` isn't set.

    '''
    uds_dir: str|None = get_uds_dir()
    # snapshot pre-test sock-file population so we only
    # blame this test for files it added (others may have
    # been left around by session-scoped fixtures /
    # cross-session leaks pending reaper).
    before: set[str] = set()
    if uds_dir:
        try:
            before = {
                e for e in os.listdir(uds_dir)
                if e.endswith('.sock')
            }
        except OSError:
            pass

    yield

    if not uds_dir:
        return
    try:
        after: set[str] = {
            e for e in os.listdir(uds_dir)
            if e.endswith('.sock')
        }
    except OSError:
        return
    new_files: set[str] = after - before
    if not new_files:
        return
    # only consider files whose binder pid is dead (or the
    # 1616 sentinel) — a still-running test that legit
    # holds a sock open will be ignored here and caught at
    # session-end if it really is leaked.
    orphans: list[str] = find_orphaned_uds(uds_dir=uds_dir)
    new_orphans: list[str] = [
        os.path.join(uds_dir, n) for n in new_files
        if os.path.join(uds_dir, n) in orphans
    ]
    if new_orphans:
        import warnings
        warnings.warn(
            'UDS sock-file LEAK detected from test '
            '(reaping):\n  '
            + '\n  '.join(new_orphans),
            stacklevel=1,
        )
        reap_uds(new_orphans)


@pytest.fixture(
    scope='function',
    autouse=True,
)
def _detect_runaway_subactors_per_test():
    '''
    Per-test (function-scoped) autouse runaway-subactor
    detector.

    Snapshots descendant pids before+after each test;
    for any pid spawned during the test that's still
    ALIVE at teardown AND burning >95% CPU, emits a loud
    warning with `pid`, sampled `cpu%`, full `cmdline`,
    AND copy-pastable diag commands (`strace`, `lsof`,
    `ss`, `kill`).

    **Does NOT kill the runaway** — by design.
    The point of this fixture is to make tight-loop bugs
    (e.g. C-level `recvfrom` loop on a closed socket
    that missed EOF detection — see
    `ai/conc-anal/trio_wakeup_socketpair_busy_loop_under_fork_issue.md`)
    loudly visible AT the test that triggers, while
    keeping the live pid available for hands-on
    diagnosis. The session-end
    `_reap_orphaned_subactors` fixture will
    SIGINT-then-SIGKILL any survivors when the test
    session completes normally; if the user Ctrl-C's
    pytest mid-warning, the pid stays alive for as long
    as needed.

    Cost: one extra `os.listdir('/proc')` snapshot
    pre-test, one snapshot + N×`psutil.cpu_percent(0.5)`
    post-test (only when there ARE new descendants —
    most tests don't trigger any sampling). Skips
    silently when `psutil` isn't installed.

    '''
    parent_pid: int = os.getpid()

    def _emit_runaway_warning(
        runaways: list[tuple[int, float, str]],
        when: str,
    ) -> None:
        '''
        Format + emit the runaway warning. Shared between
        the SETUP-side (pre-yield, catches survivors of a
        prior hung test) and TEARDOWN-side (post-yield,
        catches normally-completing tests that left a
        runaway behind) detection passes.

        '''
        msg_lines: list[str] = [
            f'RUNAWAY subactor(s) detected at {when} — '
            f'burning CPU (>95%):',
        ]
        for pid, cpu, cmdline in runaways:
            msg_lines.extend([(
                f'  pid={pid} cpu={cpu:.1f}% cmdline={cmdline!r}\n'
                f'  diagnose live (pid stays alive — NOT killed):\n'
                f'    sudo strace -p {pid} -f -tt -e trace=recvfrom,epoll_wait,read,write\n'
                f'    sudo readlink /proc/{pid}/fd/* 2>/dev/null | head -20\n'
                f'    sudo ss -tnp | grep {pid}\n'
                f'    sudo lsof -p {pid}\n'
                f'  manual kill when done:\n'
                f'    kill -SIGINT {pid}    # graceful first\n'
                f'    kill -SIGKILL {pid}   # if SIGINT ignored (busy in C)\n'
                f'\n'
            )])
        import warnings
        warnings.warn(
            '\n'.join(msg_lines),
            stacklevel=1,
        )

    # SETUP-side detection: catches runaways inherited
    # from a PRIOR test that hung (and the user
    # Ctrl-C'd or pytest-timeout fired) — those tests'
    # teardown-side detector never ran, but the
    # subactor is still burning CPU when the next test
    # starts. The warning comes ONE TEST LATE which is
    # imperfect but better than silence.
    pre_existing: set[int] = set(find_descendants(parent_pid))
    pre_runaways: list[tuple[int, float, str]] = (
        find_runaway_subactors(
            parent_pid,
            only_pids=pre_existing,
        )
    )
    if pre_runaways:
        _emit_runaway_warning(
            pre_runaways,
            when='test SETUP (leftover from prior hung test)',
        )

    yield

    # TEARDOWN-side detection: catches runaways spawned
    # by THIS test that survived a normal teardown
    # (i.e. parent's `hard_kill` SIGKILL didn't actually
    # stop the runaway because it was in C tight-loop
    # somewhere unreachable to signals — see
    # `ai/conc-anal/trio_wakeup_socketpair_busy_loop_under_fork_issue.md`
    # for the canonical fork-spawn forkserver-worker
    # post-fork-close gap).
    post_runaways: list[tuple[int, float, str]] = (
        find_runaway_subactors(
            parent_pid,
            only_pids=set(
                find_descendants(parent_pid)
            ) - pre_existing,
        )
    )
    if post_runaways:
        _emit_runaway_warning(
            post_runaways,
            when='test teardown',
        )


@pytest.fixture
def reap_subactors_per_test() -> int:
    '''
    Per-test (function-scoped) zombie-subactor reaper —
    **opt-in**, NOT autouse.

    When a test's teardown fails to fully cancel its actor
    tree (e.g. an asyncio cancel-cascade times out under
    `main_thread_forkserver`, pytest hits its 200s wall-
    clock and abandons), the leftover subactor lingers as a
    direct child of `pytest` and squats on whatever
    registrar port / UDS path / shm segment it had bound.
    Subsequent tests trying to allocate the same resource
    fail — and with backends that bind a session-shared
    `reg_addr`, that means EVERY following test in the
    suite cascades. The session-scoped sibling
    (`_reap_orphaned_subactors`) only kicks in at session
    end which is too late to save the cascade.

    Apply at module-level on the topically-problematic
    test files via:

    ```python
    pytestmark = pytest.mark.usefixtures(
        'reap_subactors_per_test',
    )
    ```

    Or per-test via the same `usefixtures` mark on a
    specific function. Intentionally NOT autouse so the
    fixture's presence on a module signals "this module's
    teardown is known-leaky enough to contaminate
    siblings"; the visibility helps future-us track down
    root causes rather than burying them under blanket
    cleanup.

    '''
    parent_pid: int = os.getpid()
    yield parent_pid
    pids: list[int] = find_descendants(parent_pid)
    if pids:
        reap(pids, grace=3.0)
