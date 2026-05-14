# tractor: distributed structured concurrency.
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
Pure-Python diagnostic state-capture for hung
`pytest`/`tractor` process trees.

This module is the load-bearing core for two consumers:

1. The `xontrib/tractor_diag.xsh::acli.*` xonsh aliases
   (`acli.ptree`, `acli.hung_dump`, `acli.bindspace_scan`,
   `acli.dump_all`) — interactive terminal diag tools.

2. In-test "capture-on-hang" helpers like
   `fail_after_w_trace()` / `afk_alarm_w_trace()` that drop a
   full diag snapshot to disk when a test exceeds its timeout
   budget instead of just emitting a context-less
   `trio.TooSlowError`.

All public dump-* functions RETURN formatted text rather than
printing, so callers can render to a terminal OR write to a
file. `dump_all()` does the file-writing for snapshot-archive
use cases.

Sudo policy:
  Per-pid kernel `stack` + `py-spy dump` need `CAP_SYS_PTRACE`,
  invoked via `sudo -n`. Two modes:

  - `allow_sudo_prompt=True` (terminal CLI default):
    `ensure_sudo_cached()` prompts the user once via `sudo -v`
    if creds aren't cached, then re-uses them per-pid.

  - `allow_sudo_prompt=False` (pytest / in-test default):
    silently skip sudo-required diagnostics; emit a banner
    pointing the human at `sudo -v && acli.hung_dump <pid>`
    for a follow-up manual capture.

'''
from __future__ import annotations

import json
import os
import re
import signal
import subprocess as sp
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
)
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import (
    AsyncIterator,
    Callable,
    Iterator,
    TypeAlias,
)


# Public type aliases for the `fail_after_w_trace` /
# `afk_alarm_w_trace` fixture-returned CM-factory callables.
# Test signatures can annotate the fixture param directly::
#
#     def test_foo(
#         fail_after_w_trace: FailAfterWTraceFactory,
#     ):
#         async with fail_after_w_trace(5.0):
#             ...
#
# NOTE the fixture name intentionally shadows the underlying
# `fail_after_w_trace` function at test-fn scope; pytest's
# param-resolution overrides the module-level import, so the
# fixture-returned CM-factory wins inside the test body.
#
# `Callable[..., ...]` keeps the kwargs surface loose (caller
# can pass `label=`, `pid=`, `out_dir=`); precise checking of
# the first-arg `seconds` is left to runtime since most callers
# pass an `int|float` literal.
FailAfterWTraceFactory: TypeAlias = Callable[
    ...,
    AbstractAsyncContextManager[None],
]
AfkAlarmWTraceFactory: TypeAlias = Callable[
    ...,
    AbstractContextManager[None],
]

try:
    import psutil
except ImportError:
    psutil = None

try:
    import pytest as _pytest
except ImportError:
    # `trace.py`'s pure-Python core (proc-tree + bindspace +
    # dump_*) is intentionally pytest-free so the `xontrib`
    # CLI can `import` it from any venv. The fixtures at
    # the bottom of this module require `pytest` and are
    # only defined when it's importable.
    _pytest = None


# matches tractor's UDS sock naming: `<actor_name>@<pid>.sock`
_UDS_SOCK_RE = re.compile(
    r'^(?P<name>.+)@(?P<pid>\d+)\.sock$'
)


# ---------------------------------------------------------------
# pid + proc-tree resolution
# ---------------------------------------------------------------

def resolve_pids(arg: str) -> list[int]:
    '''
    Resolve a numeric pid OR a `pgrep -f` pattern to a list of
    pids. Returns `[]` on no match.

    '''
    if arg.isdigit():
        return [int(arg)]
    try:
        out: str = sp.check_output(
            ['pgrep', '-f', arg],
            text=True,
        )
    except sp.CalledProcessError:
        return []
    return [int(p) for p in out.split() if p]


def walk_tree_psutil(pid: int) -> list:
    '''Flat `[Process, *descendants]` via `psutil` (or `[]`).'''
    if psutil is None:
        return []
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    return [p] + p.children(recursive=True)


def _walk_tree_with_depth(pid: int) -> Iterator[tuple]:
    '''Yield `(proc, depth)` pairs walking `pid`'s subtree.'''
    if psutil is None:
        return
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    yield root, 0
    stack: list = [(root, 0)]
    seen: set = {pid}
    while stack:
        parent, d = stack.pop()
        try:
            kids = parent.children()
        except psutil.NoSuchProcess:
            continue
        for k in kids:
            if k.pid in seen:
                continue
            seen.add(k.pid)
            yield k, d + 1
            stack.append((k, d + 1))


def _walk_tree_pgrep(pid: int) -> list[int]:
    '''psutil-less fallback — recursive `pgrep -P`.'''
    out: list[int] = [pid]
    try:
        kids: list = sp.check_output(
            ['pgrep', '-P', str(pid)],
            text=True,
        ).split()
    except sp.CalledProcessError:
        return out
    for k in kids:
        out.extend(_walk_tree_pgrep(int(k)))
    return out


def _which_cgroup_slice(pid: int) -> str | None:
    '''
    Return `'system'` / `'user'` / `None` for `pid`'s top-level
    systemd cgroup slice. See the full `xontrib` docstring on
    `_which_cgroup_slice` for the bucket semantics.

    '''
    try:
        with open(f'/proc/{pid}/cgroup') as f:
            cg: str = f.read()
    except (
        FileNotFoundError,
        PermissionError,
        ProcessLookupError,
        OSError,
    ):
        return None
    if '/system.slice/' in cg:
        return 'system'
    if '/user.slice/' in cg:
        return 'user'
    return None


def _find_tractor_strays(seen: set[int]) -> list[int]:
    '''
    Scan `/proc/*/cmdline` (+ `/comm` as zombie-safe fallback) for
    `tractor._child` / `tractor[<aid>]` proctitle matches whose
    `pid` is NOT in the `seen` set AND whose `ppid` disposition
    indicates the proc belongs to THIS test run's process tree:

      - `ppid == 1`  → init-adopted (parent died) — a real leaked
        subactor from this (or a prior killed) test run.
      - `ppid in seen` → subtree-descendant the recursive walk
        missed due to a race (proc appeared between iterations).

    Procs whose `ppid` points to some OTHER live, non-pytest
    process are skipped — they belong to a different tractor app
    (e.g. `piker`, another `pytest` invocation in another shell,
    a long-running tractor daemon) and falsely flagging them as
    "cross-test ghosts" of THIS run is misleading.

    Used by `dump_proc_tree(include_strays=True)` to surface ghost
    subactor trees from PRIOR test runs that aren't descendants of
    the snapshot's root pid (typically the pytest worker). These
    are usually the source of cross-test launchpad contamination —
    e.g. orphaned `tractor._child` procs still squatting on UDS
    bindspace from a hung-then-killed pytest invocation.

    Returns the pids; caller decides what to do with them
    (typically: walk their subtrees as additional roots and let
    the existing zombie/orphan/live classification handle them).

    Reuses `_reap._is_tractor_subactor` for the cmdline/comm
    intrinsic-marker test so the detection stays in lock-step
    with the reaper's own definition.

    '''
    # lazy-imported to avoid module-import cycle: `_reap.py` is a
    # pytest plugin that imports from this module's siblings.
    from ._reap import _is_tractor_subactor

    strays: list[int] = []
    proc = Path('/proc')
    if not proc.is_dir():
        return strays
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid: int = int(entry.name)
        if pid in seen:
            continue
        if not _is_tractor_subactor(pid):
            continue
        # ownership filter: only flag procs whose `ppid` ties them
        # back to THIS test run (init-adopted orphan, or a
        # descendant the walk missed).
        ppid: int | None = _ppid_from_proc(pid)
        if ppid is None:
            # proc disappeared between `iterdir()` and `stat` —
            # treat as gone, don't flag.
            continue
        if ppid == 1 or ppid in seen:
            strays.append(pid)
    return sorted(strays)


def _ppid_from_proc(pid: int) -> int | None:
    '''
    Read `ppid` from `/proc/<pid>/stat`. Returns None on race
    (proc died) / permission / non-linux.

    NB: stat field [1] is `(comm)` which can contain spaces +
    parens — `rsplit(')', 1)` is the safe way to skip past it.

    '''
    try:
        with open(f'/proc/{pid}/stat') as f:
            stat: str = f.read()
        after_comm: str = stat.rsplit(')', 1)[1].strip()
        return int(after_comm.split()[1])  # state(0) ppid(1)
    except (
        FileNotFoundError,
        PermissionError,
        ProcessLookupError,
        OSError,
    ):
        return None


# ---------------------------------------------------------------
# sudo probe / prompt
# ---------------------------------------------------------------

def is_sudo_cached() -> bool:
    '''
    Quietly probe whether `sudo` creds are cached. Never
    prompts — safe to call from non-interactive contexts.

    '''
    try:
        return sp.run(
            ['sudo', '-n', 'true'],
            capture_output=True,
        ).returncode == 0
    except FileNotFoundError:
        return False


def ensure_sudo_cached() -> bool:
    '''
    Like `is_sudo_cached()` but PROMPTS interactively via
    `sudo -v` if not yet cached. Suitable for terminal-CLI use
    only — DO NOT call from inside a pytest run.

    '''
    if is_sudo_cached():
        return True
    print(
        '[tractor-trace] needs `sudo` for '
        '/proc/<pid>/stack and `py-spy dump`; caching creds '
        'via `sudo -v`...'
    )
    try:
        rc: int = sp.run(['sudo', '-v']).returncode
    except KeyboardInterrupt:
        print('  cancelled — proceeding without sudo')
        return False
    except FileNotFoundError:
        print('  sudo not on PATH — proceeding without sudo')
        return False
    return rc == 0


# ---------------------------------------------------------------
# dump_proc_tree (== acli.ptree)
# ---------------------------------------------------------------

def dump_proc_tree(
    roots: list[int],
    *,
    flag_tree: bool = False,
    include_strays: bool = True,
) -> str:
    '''
    Severity-classified proc-tree rendering of `roots` and
    their descendants. Returns formatted text.

    Buckets (severity-ordered):
      - zombies:       `status in (Z, X)`
      - orphans:       `ppid==1`, NOT in a systemd cgroup slice
      - system-slice:  `ppid==1`, under `/system.slice/`
      - user-slice:    `ppid==1`, under `/user.slice/.../*.scope`
      - live:          real (`ppid > 1`) parent

    `flag_tree=True` additionally prepends a flat walk-order
    `## tree` section preserving parent-child shape.

    `include_strays=True` (default) additionally scans
    `/proc/*/cmdline` for `tractor._child` / `tractor[<aid>]`
    procs that are NOT descendants of any provided root — these
    are typically ghost subactor trees from PRIOR test runs
    (cross-test launchpad contamination). Their subtrees are
    walked and classified normally; the bucket counts then
    include them. See `_find_tractor_strays()`.

    '''
    buf = StringIO()

    def echo(line: str = '') -> None:
        buf.write(line + '\n')

    if psutil is None:
        echo(
            'ptree requires `psutil`; '
            'install via `uv pip install psutil`'
        )
        return buf.getvalue()

    # statuses considered "defunct"
    defunct_statuses: set = {
        psutil.STATUS_ZOMBIE,
        getattr(psutil, 'STATUS_DEAD', 'dead'),
    }

    seen: set = set()
    walk_order: list = []
    live: list = []
    orphans: list = []
    system_slice: list = []
    user_slice: list = []
    zombies: list = []
    gone: list = []
    pid_to_bucket: dict = {}

    # lazy-imported, used to override cgroup-slice classification
    # for `tractor._child` strays (they're orphans regardless of
    # whether they happen to be in the user.slice / system.slice
    # cgroup — `desktop-launched app` is the *wrong* read for a
    # leaked subactor that just happens to inherit user-session
    # cgroup membership from its now-dead parent).
    from ._reap import _is_tractor_subactor

    def _classify_walk(walk_roots: list[int]) -> None:
        '''Walk + classify into the closure-shared bucket lists.'''
        for r in walk_roots:
            for (p, depth) in _walk_tree_with_depth(r):
                if p.pid in seen:
                    continue
                seen.add(p.pid)
                try:
                    status: str = p.status()
                    ppid: int = p.ppid()
                except psutil.NoSuchProcess:
                    gone.append(p.pid)
                    continue
                entry = (p, depth)
                if status in defunct_statuses:
                    zombies.append(entry)
                    pid_to_bucket[p.pid] = 'zombies'
                elif ppid == 1:
                    # `tractor._child` procs reparented to init are
                    # leaked subactors regardless of cgroup-slice —
                    # short-circuit to `orphans` before falling back
                    # to the systemd-slice categorization (which is
                    # only meaningful for NON-tractor procs).
                    if _is_tractor_subactor(p.pid):
                        orphans.append(entry)
                        pid_to_bucket[p.pid] = 'orphans'
                    else:
                        slice_kind: str | None = _which_cgroup_slice(p.pid)
                        if slice_kind == 'system':
                            system_slice.append(entry)
                            pid_to_bucket[p.pid] = 'system-slice'
                        elif slice_kind == 'user':
                            user_slice.append(entry)
                            pid_to_bucket[p.pid] = 'user-slice'
                        else:
                            orphans.append(entry)
                            pid_to_bucket[p.pid] = 'orphans'
                else:
                    live.append(entry)
                    pid_to_bucket[p.pid] = 'live'
                walk_order.append(entry)

    _classify_walk(roots)
    explicit_seen: set = set(seen)

    stray_roots: list[int] = []
    if include_strays:
        stray_roots = _find_tractor_strays(seen)
        if stray_roots:
            _classify_walk(stray_roots)

    total: int = (
        len(live)
        + len(orphans)
        + len(system_slice)
        + len(user_slice)
        + len(zombies)
    )
    echo(f'# ptree: {total} procs across roots {roots}')
    if stray_roots:
        n_stray_proc: int = len(seen) - len(explicit_seen)
        echo(
            f'#  + {n_stray_proc} `tractor._child` stray proc(s) '
            f'NOT descendants of {roots} '
            f'(likely cross-test ghosts; see bindspace dump for '
            f'their UDS sock state):'
        )
        for sr in stray_roots:
            echo(f'#    stray-root: {sr}')

    hdr: str = (
        '  ' + 'PID'.rjust(7)
        + '  ' + 'PPID'.rjust(7)
        + '  ' + 'STATUS'.ljust(10)
        + '  CMD'
    )

    def _row(entry, bucket: str | None = None) -> str:
        p, depth = entry
        tree_pfx: str = ('   ' * depth) + ('└─ ' if depth > 0 else '')

        parent_anno: str = ''
        if (
            bucket is not None
            and depth > 0
        ):
            try:
                parent_pid: int = p.ppid()
            except psutil.NoSuchProcess:
                parent_pid = 0
            if parent_pid and parent_pid != 1:
                parent_bucket: str | None = pid_to_bucket.get(parent_pid)
                if (
                    parent_bucket is not None
                    and parent_bucket != bucket
                ):
                    parent_anno = (
                        f'  [parent: {parent_pid} '
                        f'(in `{parent_bucket}`)]'
                    )

        try:
            cmd: str = (
                ' '.join(p.cmdline())[:140]
                or '[' + p.name() + ']'
            )
            r: str = '  ' + str(p.pid).rjust(7)
            r += '  ' + str(p.ppid()).rjust(7)
            r += '  ' + p.status().ljust(10)
            r += '  ' + tree_pfx + cmd + parent_anno
            return r
        except psutil.ZombieProcess:
            try:
                ppid_str: str = str(p.ppid())
                name: str = p.name()
            except psutil.NoSuchProcess:
                ppid_str, name = '?', '?'
            r = '  ' + str(p.pid).rjust(7)
            r += '  ' + ppid_str.rjust(7)
            r += '  ' + 'zombie'.ljust(10)
            r += (
                '  ' + tree_pfx
                + '[' + name + ' <defunct>]'
                + parent_anno
            )
            return r
        except psutil.NoSuchProcess:
            return (
                '  ' + str(p.pid).rjust(7)
                + '  (gone mid-walk)'
            )

    def _section(
        title: str,
        procs: list,
        hint: str = '',
        bucket: str | None = None,
    ) -> None:
        echo()
        echo(
            f'## {title} ({len(procs)})'
            + (f'  — {hint}' if hint else '')
        )
        if not procs:
            echo('  (none)')
            return
        echo(hdr)
        for p in procs:
            echo(_row(p, bucket=bucket))

    if flag_tree:
        _section(
            'tree', walk_order,
            'flat walk-order, parent-child preserved',
        )

    _section(
        'zombies', zombies,
        'status `Z`/`X`, parent has not reaped',
        bucket='zombies',
    )
    _section(
        'orphans', orphans,
        '`ppid==1` + leaked: either NOT in a `system.slice`/'
        '`user.slice` cgroup, OR a known `tractor._child` '
        'proc (leaked subactor, regardless of cgroup-slice)',
        bucket='orphans',
    )
    _section(
        'system-slice', system_slice,
        '`ppid==1`, rooted under `/system.slice/` '
        '(real systemd-managed service — daemon, login '
        'session manager, etc; not a leak)',
        bucket='system-slice',
    )
    _section(
        'user-slice', user_slice,
        '`ppid==1`, rooted under `/user.slice/.../*.scope` '
        '(desktop-launched app wrapped by systemd-user — '
        'browser, editor, etc; not a leak)',
        bucket='user-slice',
    )
    _section('live', live, bucket='live')

    if gone:
        echo()
        echo(f'## gone-during-walk ({len(gone)}): {gone}')

    return buf.getvalue()


# ---------------------------------------------------------------
# dump_hung_state (== acli.hung_dump)
# ---------------------------------------------------------------

def dump_hung_state(
    roots: list[int],
    *,
    allow_sudo_prompt: bool = False,
) -> str:
    '''
    Per-pid kernel + python state for a hung pytest/tractor
    process tree. Walks descendants of each root.

    Captures per-pid:
      - `/proc/<pid>/wchan` (world-readable)
      - `/proc/<pid>/stack` (CAP_SYS_PTRACE — needs sudo)
      - `py-spy dump --pid <N> --locals` (needs sudo)

    Sudo policy controlled by `allow_sudo_prompt`:

    - `True`: call `ensure_sudo_cached()` which prompts via
      `sudo -v` if creds aren't cached. Use from terminal CLI.

    - `False` (default): only probe via `is_sudo_cached()` —
      never prompts. If not cached, skip stack+py-spy and emit
      a banner pointing the human at the manual follow-up cmd.
      Use from inside a pytest run.

    '''
    buf = StringIO()

    def echo(line: str = '') -> None:
        buf.write(line + '\n')

    if allow_sudo_prompt:
        have_sudo: bool = ensure_sudo_cached()
    else:
        have_sudo: bool = is_sudo_cached()

    pids: list[int] = []
    seen: set = set()
    for r in roots:
        if psutil is not None:
            walk: list[int] = [p.pid for p in walk_tree_psutil(r)]
        else:
            walk = _walk_tree_pgrep(r)
        for pid in walk:
            if pid not in seen:
                seen.add(pid)
                pids.append(pid)

    echo(f'# tree: {pids}')

    if not have_sudo:
        echo()
        echo(
            '💡 sudo creds NOT cached — '
            '`/proc/<pid>/stack` + `py-spy dump` SKIPPED '
            'for all pids below.'
        )
        echo(
            '   For full kernel-stack + py-spy frames, '
            're-run manually with sudo cached:'
        )
        echo(f'     sudo -v && acli.hung_dump {pids[0] if pids else "<pid>"}')

    echo()
    echo('## ps forest')
    if pids:
        try:
            ps_out: str = sp.check_output(
                [
                    'ps',
                    '-o', 'pid,ppid,pgid,stat,cmd',
                    '-p', ','.join(map(str, pids)),
                ],
                text=True,
            )
            echo(ps_out.rstrip())
        except (sp.CalledProcessError, FileNotFoundError) as e:
            echo(f'  (ps failed: {e})')

    for pid in pids:
        echo()
        echo(f'## pid {pid}' + (
            ''
            if have_sudo
            else '  (sudo NOT cached — stack/py-spy SKIPPED)'
        ))

        for f in ('wchan', 'stack'):
            path = Path(f'/proc/{pid}/{f}')
            try:
                txt: str = path.read_text().rstrip()
                echo(f'-- /proc/{pid}/{f} --')
                echo(txt)
            except PermissionError:
                if not have_sudo:
                    echo(
                        f'-- /proc/{pid}/{f}: '
                        'PermissionError (no sudo) --'
                    )
                    continue
                try:
                    txt = sp.check_output(
                        ['sudo', '-n', 'cat', str(path)],
                        text=True,
                        stderr=sp.DEVNULL,
                    ).rstrip()
                    echo(f'-- /proc/{pid}/{f} (sudo) --')
                    echo(txt)
                except sp.CalledProcessError:
                    echo(
                        f'-- /proc/{pid}/{f}: '
                        'sudo cred expired? rerun --'
                    )
            except FileNotFoundError:
                echo(f'-- /proc/{pid}/{f}: proc gone --')

        echo(f'-- py-spy {pid} --')
        if not have_sudo:
            echo('  (skipped — no sudo)')
            continue
        try:
            py_spy_out: str = sp.check_output(
                ['sudo', '-n', 'py-spy', 'dump', '--pid', str(pid), '--locals'],
                text=True,
                stderr=sp.STDOUT,
            )
            echo(py_spy_out.rstrip())
        except (sp.CalledProcessError, FileNotFoundError) as e:
            echo(f'  (py-spy failed: {e})')

    return buf.getvalue()


# ---------------------------------------------------------------
# scan_bindspace (== acli.bindspace_scan)
# ---------------------------------------------------------------

def scan_bindspace(arg: str | None = None) -> str:
    '''
    Scan a tractor UDS bindspace dir for orphan sock files.

    `arg` semantics:
      - `None`        -> `$XDG_RUNTIME_DIR/tractor`
      - bare `<name>` -> `$XDG_RUNTIME_DIR/<name>` (e.g. `piker`)
      - path          -> use as-is

    Output buckets: `live-active`, `orphaned-alive`,
    `orphaned-dead`, `non-tractor`.

    '''
    buf = StringIO()

    def echo(line: str = '') -> None:
        buf.write(line + '\n')

    runtime: str = os.environ.get(
        'XDG_RUNTIME_DIR',
        f'/run/user/{os.getuid()}',
    )
    if arg:
        if arg.startswith('/') or '/' in arg:
            bs_dir = Path(arg)
        else:
            bs_dir = Path(runtime) / arg
    else:
        bs_dir = Path(runtime) / 'tractor'

    if not bs_dir.exists():
        echo(f'(no bindspace at {bs_dir})')
        return buf.getvalue()

    socks: list = sorted(bs_dir.glob('*.sock'))
    echo(f'## bindspace {bs_dir} ({len(socks)} sock file(s))')

    live_active: list = []
    live_orphaned: list = []
    dead_orphans: list = []
    bogus: list = []

    for s in socks:
        m = _UDS_SOCK_RE.match(s.name)
        if not m:
            bogus.append(s)
            continue
        pid = int(m['pid'])
        name = m['name']
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            dead_orphans.append((s, pid, name))
            continue
        except PermissionError:
            live_active.append((s, pid, name, None))
            continue

        ppid: int | None = _ppid_from_proc(pid)
        if ppid == 1:
            live_orphaned.append((s, pid, name, ppid))
        else:
            live_active.append((s, pid, name, ppid))

    echo()
    echo(
        f'## live-active ({len(live_active)})  '
        f'— PID alive, parent still own it'
    )
    if not live_active:
        echo('  (none)')
    for s, pid, name, ppid in live_active:
        row: str = '  ' + str(pid).rjust(7)
        row += '  ' + name.ljust(32)
        row += '  ' + s.name
        if ppid is not None:
            row += f'  (ppid={ppid})'
        echo(row)

    echo()
    echo(
        f'## orphaned-alive ({len(live_orphaned)})  '
        f'— PID alive but `ppid==1`, parent reaped; '
        f'`acli.reap` candidate'
    )
    if not live_orphaned:
        echo('  (none)')
    for s, pid, name, ppid in live_orphaned:
        row = '  ' + str(pid).rjust(7)
        row += '  ' + name.ljust(32)
        row += '  ' + s.name + '  (adopted by init)'
        echo(row)

    echo()
    echo(
        f'## orphaned-dead ({len(dead_orphans)})  '
        f'— PID gone, sock stale'
    )
    if not dead_orphans:
        echo('  (none)')
    for s, pid, name in dead_orphans:
        row = '  ' + str(pid).rjust(7)
        row += '  ' + name.ljust(32)
        row += '  ' + s.name + '  (no live proc)'
        echo(row)

    if bogus:
        echo()
        echo(
            f'## non-tractor ({len(bogus)})  '
            f'— filename lacks `@<pid>` suffix, '
            f'cannot determine liveness intrinsically'
        )
        for s in bogus:
            echo(f'  {s.name}')
        echo()
        echo('to check liveness manually (needs `iproute2`/`ss`):')
        for s in bogus:
            echo(f"  ss -lpx 'src = {s}'")

    if dead_orphans or live_orphaned:
        echo()
        echo(
            'to sweep BOTH orphaned-alive subs '
            '(graceful SIGINT -> SIGKILL) AND dead-orphan '
            'socks in one shot:'
        )
        echo('  acli.reap --uds')

    if dead_orphans:
        unlink_cmd: str = ' '.join(str(o[0]) for o in dead_orphans)
        echo()
        echo(
            '(or to unlink dead-orphan socks manually, '
            "skipping `acli.reap`'s graceful-cancel ladder:)"
        )
        echo(f'  rm {unlink_cmd}')

    return buf.getvalue()


# ---------------------------------------------------------------
# dump_all — file-writing snapshot capture
# ---------------------------------------------------------------

def _default_dump_root() -> Path:
    '''
    `$XDG_CACHE_HOME/tractor/hung-dumps/` with
    `~/.cache/tractor/hung-dumps/` fallback.

    '''
    cache: str = os.environ.get(
        'XDG_CACHE_HOME',
        str(Path.home() / '.cache'),
    )
    return Path(cache) / 'tractor' / 'hung-dumps'


def dump_all(
    pid: int,
    out_dir: Path | None = None,
    *,
    label: str,
    allow_sudo_prompt: bool = False,
) -> Path:
    '''
    Capture full diag snapshot for the proc tree rooted at
    `pid` into a new sub-directory under `out_dir`.

    Layout:
      `<out_dir>/<label>__<iso-timestamp>/`
      ├─ trace.txt        # ptree + hung_state merged
      ├─ bindspace.txt    # bindspace_scan output
      └─ meta.json        # {pid, label, captured_at, sudo_cached}

    Returns the snapshot directory `Path`.

    `out_dir` defaults to
    `$XDG_CACHE_HOME/tractor/hung-dumps/` (fallback
    `~/.cache/tractor/hung-dumps/`).

    See `dump_hung_state()` for `allow_sudo_prompt` semantics
    — defaults to False (test-safe).

    '''
    if out_dir is None:
        out_dir = _default_dump_root()
    out_dir = Path(out_dir)

    ts: str = datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
    # sanitize label for filesystem: collapse anything non-word/-./-
    # into single underscore, strip leading/trailing underscores.
    safe_label: str = re.sub(r'[^\w.\-]+', '_', label).strip('_')
    dump_dir: Path = out_dir / f'{safe_label}__{ts}'
    dump_dir.mkdir(parents=True, exist_ok=True)

    sudo_ok: bool = (
        ensure_sudo_cached()
        if allow_sudo_prompt
        else is_sudo_cached()
    )

    # combined trace.txt: ptree first (classified buckets),
    # then hung_state (per-pid wchan/stack/py-spy)
    trace_txt: str = (
        '# ===== ptree =====\n'
        + dump_proc_tree([pid])
        + '\n# ===== hung_state =====\n'
        + dump_hung_state(
            [pid],
            allow_sudo_prompt=False,  # already prompted above
        )
    )
    (dump_dir / 'trace.txt').write_text(trace_txt)

    (dump_dir / 'bindspace.txt').write_text(scan_bindspace())

    meta: dict = {
        'pid': pid,
        'label': label,
        'captured_at': ts,
        'sudo_cached': sudo_ok,
    }
    (dump_dir / 'meta.json').write_text(
        json.dumps(meta, indent=2) + '\n'
    )

    return dump_dir


# ---------------------------------------------------------------
# in-test capture-on-hang helpers
# ---------------------------------------------------------------
#
# Pair of CMs that combine a tight cooperative/hard timeout with
# a forced `dump_all()` snapshot BEFORE the failure propagates.
# The goal: when a test hangs, the human (or AI reviewer) gets a
# fresh ptree + per-pid wchan/stack + bindspace state captured to
# disk at the exact moment of the timeout — no need to recreate
# it after the fact (which is often impossible since the procs
# have moved on / been reaped).
#
# Two variants for two failure shapes:
#
#  - `fail_after_w_trace` — async CM wrapping `trio.fail_after`.
#    Cooperative: cancellation is delivered at the next trio
#    checkpoint. Use when the hang is at the trio/python level
#    and the runtime is still scheduling normally.
#
#  - `afk_alarm_w_trace` — sync CM wrapping `signal.alarm`.
#    Hard backstop: raises into the python frame at the next
#    bytecode boundary regardless of trio's state. Use as a wall-
#    clock cap when something *below* the trio scheduler is
#    locking up (e.g. forkserver-launchpad in `os.read`, native-
#    lock held by a C extension, GIL-hostage class hangs).
#    Must run on the main thread (signal.alarm constraint).
#
# Both default to dumping the CURRENT process tree (i.e. the
# pytest worker + its subactor descendants). Override `pid=` to
# scope to a specific actor root.
# ---------------------------------------------------------------


class AFKAlarmTimeout(TimeoutError):
    '''
    Raised by `afk_alarm_w_trace`'s SIGALRM handler when the
    alarm fires. Subclass of `TimeoutError` so existing
    `except TimeoutError:` catches still match.

    '''


# Session-scoped list of snapshot (label, dump_dir) tuples
# captured by `fail_after_w_trace` / `afk_alarm_w_trace` during
# the current process lifetime. Populated by
# `_do_capture_snapshot()` on each successful dump. The
# `pytest_terminal_summary` hook in `tractor._testing.pytest`
# reads this at end-of-session to print an index of all
# snapshot dirs so the human doesn't have to scroll back through
# captured-stderr lines to find paths.
_SNAPSHOT_INDEX: list[tuple[str, Path]] = []


# TODO: follow-up — `TRACTOR_TRACE_HOLD=1` pause-on-hang mode.
# When env-var-enabled, `_do_capture_snapshot` would block on
# `input('press Enter to continue...')` reading from
# `sys.__stdin__` AFTER the dump succeeds, BEFORE re-raising the
# original exception. This lets a human invoke
# `acli.ptree`/`acli.bindspace_scan` from a second terminal
# while the cancel-cascade is frozen mid-flight — currently
# impossible because the per-test reaper fixture sweeps
# orphans within ~0.6s of the timeout firing. See discussion
# 2026-05-13: orphans visible in snapshot's `trace.txt`
# (depth_3 / depth_1 init-adopted procs) but invisible to any
# post-test `acli.*` invocation.


def _do_capture_snapshot(
    *,
    label: str,
    pid: int | None,
    out_dir: Path | None,
    seconds: float,
    timeout_kind: str,  # 'fail_after' | 'afk_alarm'
) -> Path | None:
    '''
    Run `dump_all()` inside a best-effort try-block — never let
    capture failure mask the original timeout exception.

    Returns the snapshot `Path` on success, `None` if capture
    itself failed (with a banner printed to stderr).

    Appends `(label, dump_dir)` to the session-scoped
    `_SNAPSHOT_INDEX` on success so the `pytest_terminal_summary`
    hook can render an index at end-of-session.

    '''
    target_pid: int = pid if pid is not None else os.getpid()
    # NOTE: print to `sys.__stderr__` (the ORIGINAL unredirected
    # stderr) rather than `sys.stderr` so the snapshot-path message
    # bypasses pytest's `--capture=sys` redirection. Under pytest
    # xfailed/passed tests have their captured streams SUPPRESSED
    # entirely (and `--show-capture` only affects FAILED tests),
    # so writing to `sys.stderr` would hide the diag info from the
    # human running the suite. `__stderr__` is the pre-capture fd,
    # always lands on the real terminal. Outside pytest (e.g. the
    # xontrib CLI), `sys.__stderr__ is sys.stderr` so no difference.
    import sys

    try:
        dump_dir: Path = dump_all(
            target_pid,
            out_dir=out_dir,
            label=label,
            # in-test default: never prompt for sudo (would
            # deadlock pytest); the dump_hung_state banner
            # points the human at `sudo -v && acli.hung_dump`
            # for a follow-up manual capture.
            allow_sudo_prompt=False,
        )
    except Exception as e:
        print(
            f'[{timeout_kind}_w_trace] '
            f'⚠️  dump_all() failed: {e!r} '
            f'(label={label!r}, pid={target_pid})',
            file=sys.__stderr__,
        )
        return None

    print(
        f'[{timeout_kind}_w_trace] '
        f'⏰ timed out after {seconds}s (label={label!r}, '
        f'pid={target_pid}); snapshot at: {dump_dir}',
        file=sys.__stderr__,
    )
    _SNAPSHOT_INDEX.append((label, dump_dir))
    return dump_dir


@asynccontextmanager
async def fail_after_w_trace(
    seconds: float,
    *,
    label: str,
    pid: int | None = None,
    out_dir: Path | None = None,
) -> AsyncIterator[None]:
    '''
    Async CM: `trio.fail_after(seconds)` + on-timeout
    `dump_all()` snapshot BEFORE the `trio.TooSlowError`
    propagates.

    Parameters
    ----------
    seconds:
        timeout budget passed to `trio.fail_after`.
    label:
        snapshot dir prefix (e.g. test name).
    pid:
        root pid to snapshot. Defaults to current process —
        which under pytest is the test worker, and its
        descendants are the spawned subactor tree.
    out_dir:
        snapshot parent dir. Defaults to
        `$XDG_CACHE_HOME/tractor/hung-dumps/`.

    Snapshot is taken in EITHER of two cases:
      1. `trio.fail_after` raises `TooSlowError` at scope-
         exit (body returned cleanly but past the deadline).
      2. The body raised a non-`TooSlowError` exception AFTER
         our scope's cancel had been triggered — e.g. an
         `open_nursery.__aexit__` wraps the timeout-induced
         `Cancelled` into a `BaseExceptionGroup` and that
         BEG escapes BEFORE `trio.fail_after`'s exit-check
         can raise `TooSlowError`. Without this branch the
         BEG would propagate untouched and no diag would be
         captured.

    The captured dump is best-effort (failure is logged to
    stderr but doesn't mask the original exception). The
    original exception always propagates.

    Example
    -------
    >>> async with fail_after_w_trace(
    ...     5.0,
    ...     label='test_multierror_fast_nursery',
    ... ):
    ...     await some_hangy_thing()

    '''
    # local import — trio is a hard dep of tractor, but the
    # rest of `trace.py` is trio-free (used from xontrib cli).
    # Keeping the import scoped here means `trace.py` stays
    # importable from a plain-python REPL.
    import trio

    captured: bool = False
    try:
        with trio.fail_after(seconds) as scope:
            try:
                yield
            except BaseException:
                # Body raised. If our `fail_after`'s scope had
                # already cancelled (e.g. deadline hit and a
                # nursery `__aexit__` wrapped the resulting
                # `Cancelled` into a `BaseExceptionGroup`), the
                # body's exc is downstream of OUR timeout —
                # capture diag now since `trio.fail_after`'s
                # `TooSlowError` re-raise won't fire when a
                # different exc is in flight.
                if scope.cancel_called:
                    _do_capture_snapshot(
                        label=label,
                        pid=pid,
                        out_dir=out_dir,
                        seconds=seconds,
                        timeout_kind='fail_after',
                    )
                    captured = True
                raise
    except trio.TooSlowError:
        # Body finished without raising; `fail_after`'s exit-
        # check fired `TooSlowError`.
        if not captured:
            _do_capture_snapshot(
                label=label,
                pid=pid,
                out_dir=out_dir,
                seconds=seconds,
                timeout_kind='fail_after',
            )
        raise


@contextmanager
def afk_alarm_w_trace(
    seconds: int,
    *,
    label: str,
    pid: int | None = None,
    out_dir: Path | None = None,
) -> Iterator[None]:
    '''
    Sync CM: arm `signal.alarm(seconds)`, on SIGALRM fire
    `dump_all()` then raise `AFKAlarmTimeout` so the test
    fails.

    Hard-kill backstop for cases where `trio.fail_after`
    cannot deliver cancellation — e.g. python-level GIL-
    hostage hangs, native locks held by C extensions, or a
    forkserver-launchpad parked in `os.read()`.

    Constraints
    -----------
    - Must be invoked from the MAIN thread (`signal.alarm`
      can only be armed on main thread).
    - Cannot be nested with other SIGALRM consumers — the
      previous handler is restored on exit, but two
      overlapping `afk_alarm` CMs will clobber each other.

    Parameters mirror `fail_after_w_trace`. `seconds` is
    clamped to integer (signal.alarm granularity).

    Example
    -------
    >>> with afk_alarm_w_trace(
    ...     60, label='test_sigint_closes_lifetime_stack',
    ... ):
    ...     trio.run(main)

    '''
    seconds_int: int = max(1, int(seconds))

    def _handler(signum, frame):
        raise AFKAlarmTimeout(
            f'afk_alarm fired after {seconds_int}s '
            f'(label={label!r})'
        )

    prev_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds_int)
    try:
        yield
        signal.alarm(0)  # disarm on clean exit
    except AFKAlarmTimeout:
        # alarm already self-cleared; capture diag + re-raise
        _do_capture_snapshot(
            label=label,
            pid=pid,
            out_dir=out_dir,
            seconds=seconds_int,
            timeout_kind='afk_alarm',
        )
        raise
    finally:
        # belt-and-suspenders: ensure alarm is disarmed even
        # on non-alarm exception paths (e.g. test failed for a
        # different reason inside the body).
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev_handler)


# ---------------------------------------------------------------
# pytest fixture wrappers
# ---------------------------------------------------------------
# Pre-bind the snapshot `label=` to `request.node.name` so tests
# don't have to plumb `request: pytest.FixtureRequest` AND
# `label=request.node.name` through every call site.
#
# Re-exported from `tractor._testing.pytest` so they're picked up
# by pytest's plugin-discovery (per the `pytest_plugins` entry in
# `pyproject.toml`'s `[tool.pytest.ini_options]`).
# ---------------------------------------------------------------

if _pytest is not None:

    @_pytest.fixture(name='fail_after_w_trace')
    def fail_after_w_trace_fixture(
        request: _pytest.FixtureRequest,
    ) -> FailAfterWTraceFactory:
        '''
        Pre-labeled async-CM factory for
        `fail_after_w_trace`.

        Auto-injects `label=request.node.name` so tests just
        do::

            async def test_foo(
                fail_after_w_trace: FailAfterWTraceFactory,
            ):
                async with fail_after_w_trace(5.0):
                    await some_hangy_thing()

        instead of the more verbose::

            async def test_foo(request):
                async with fail_after_w_trace(
                    5.0, label=request.node.name,
                ):
                    ...

        Any kwarg can still be overridden by the caller (e.g.
        a custom `label=` for hand-tuned dedup of snapshot
        dirs when parametrize ids aren't discriminating
        enough).

        '''
        @asynccontextmanager
        async def _bound(seconds, **kwargs):
            kwargs.setdefault('label', request.node.name)
            async with fail_after_w_trace(seconds, **kwargs):
                yield

        return _bound

    @_pytest.fixture(name='afk_alarm_w_trace')
    def afk_alarm_w_trace_fixture(
        request: _pytest.FixtureRequest,
    ) -> AfkAlarmWTraceFactory:
        '''
        Pre-labeled sync-CM factory for `afk_alarm_w_trace`.

        Sync sibling of `fail_after_w_trace` — wraps the
        SIGALRM-based hard wall-clock backstop with auto-
        injected `label=request.node.name`::

            def test_foo(
                afk_alarm_w_trace: AfkAlarmWTraceFactory,
            ):
                with afk_alarm_w_trace(10):
                    trio.run(main)

        See `afk_alarm_w_trace` for constraints (must run on
        main thread; clobbers other SIGALRM consumers).

        '''
        @contextmanager
        def _bound(seconds, **kwargs):
            kwargs.setdefault('label', request.node.name)
            with afk_alarm_w_trace(seconds, **kwargs):
                yield

        return _bound
