"""
`xontrib_tractor_diag`: pytest/tractor diagnostic aliases.

All aliases live under the `acli.` namespace so xonsh's
prefix-completion treats them as a sub-cmd group — type
`acli.<TAB>` to see the full set.

Provides:
  - `acli.pytree <pid|pgrep-pat>`       psutil-backed proc tree,
                                        live + zombies split.
  - `acli.hung_dump <pid|pat> [...]`    kernel `wchan`/`stack` +
                                        `py-spy dump` (incl `--locals`)
                                        for each pid in tree.
  - `acli.bindspace_scan [<dir>]`       find orphaned tractor UDS
                                        sock files (no live owner pid).
                                        default: `$XDG_RUNTIME_DIR/tractor`.
  - `acli.reap [opts]`                  SC-polite zombie-subactor
                                        reaper + optional `/dev/shm/`
                                        + UDS sock-file sweeps.
                                        alias for `scripts/tractor-reap`.

Loading from repo root:
  xontrib load -p ./xontrib tractor_diag

Or source directly:
  source ./xontrib/tractor_diag.xsh

Pipe-to-paste idiom (xonsh):
  hung-dump pytest |t /tmp/hung.log

Requires `psutil` for full functionality (`pytree` and the
`hung-dump` tree-walk). Falls back to `pgrep -P` recursion
if missing.
"""

import os
import re
import subprocess as sp
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None
    print(
        '[tractor-diag] `psutil` missing — '
        'acli.pytree disabled, acli.hung_dump uses pgrep fallback. '
        '`uv pip install psutil` for full functionality.'
    )


# matches tractor's UDS sock naming: `<actor_name>@<pid>.sock`
_UDS_SOCK_RE = re.compile(
    r'^(?P<name>.+)@(?P<pid>\d+)\.sock$'
)


# --- helpers --------------------------------------------------

def _resolve_pids(arg: str) -> list:
    '''Resolve a numeric pid OR a `pgrep -f` pattern.'''
    if arg.isdigit():
        return [int(arg)]
    try:
        out = sp.check_output(
            ['pgrep', '-f', arg],
            text=True,
        )
    except sp.CalledProcessError:
        return []
    return [int(p) for p in out.split() if p]


def _walk_tree_psutil(pid: int) -> list:
    '''Flat list `[Process, *descendants]` via psutil.'''
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    return [p] + p.children(recursive=True)


def _walk_tree_with_depth(pid: int):
    '''
    Yield `(proc, depth)` pairs walking `pid`'s tree. `depth==0`
    is the root; `depth==1` are direct children, etc. Used by
    `pytree` to render parent/child relationships visually.
    '''
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


def _walk_tree_pgrep(pid: int) -> list:
    '''psutil-less fallback — recursive `pgrep -P`.'''
    out = [pid]
    try:
        kids = sp.check_output(
            ['pgrep', '-P', str(pid)],
            text=True,
        ).split()
    except sp.CalledProcessError:
        return out
    for k in kids:
        out.extend(_walk_tree_pgrep(int(k)))
    return out


def _ensure_sudo_cached() -> bool:
    '''
    Ensure `sudo` credentials are cached so subsequent
    `sudo -n` calls succeed without prompting.

    Returns True if cached (or successfully refreshed),
    False if user cancelled or sudo is unavailable.

    Tries `sudo -n true` first as a no-op probe; if that
    fails, runs `sudo -v` which prompts interactively to
    validate/refresh the credential timestamp.
    '''
    # probe — already cached?
    cached = sp.run(
        ['sudo', '-n', 'true'],
        capture_output=True,
    ).returncode == 0
    if cached:
        return True

    print(
        '[tractor-diag] needs `sudo` for /proc/<pid>/stack '
        'and `py-spy dump`; caching creds via `sudo -v`...'
    )
    try:
        rc = sp.run(['sudo', '-v']).returncode
    except KeyboardInterrupt:
        print('  cancelled — proceeding without sudo')
        return False
    except FileNotFoundError:
        print('  sudo not on PATH — proceeding without sudo')
        return False
    return rc == 0


# --- pytree ---------------------------------------------------

def _pytree(args):
    '''
    psutil-backed proc tree; per-proc classification into
    severity-ordered buckets so leaked / defunct procs
    don't hide in the noise of normal `live` rows.

    usage: acli.pytree [--tree|-t] <pid|pgrep-pattern> [...]

    classification (per-proc, not per-tree):

      - zombies:  `status in (Z, X)` — defunct, parent
                  hasn't reaped (or kernel-marked dead).
      - orphans:  `ppid == 1` — original parent exited;
                  has been reparented to init. Includes
                  the *root* of an abandoned tree AND
                  any descendant that ended up reparented
                  to init mid-flight.
      - live:     real parent (`ppid > 1`), non-defunct.

    Trees of orphan roots are still walked — their
    descendants show as `live` if they themselves still
    have a real (non-init) parent (the orphan root), but
    the orphan root itself appears in `orphans`.

    Cross-bucket parent annotation (always emitted):
      when a row's parent (by ppid) lives in a *different*
      severity bucket, the row is suffixed with
      `[parent: <pid> (in `<bucket>`)]` so the visual
      `└─` marker still resolves to a findable parent
      even when bucketing scatters parent and child into
      separate sections.

    `--tree` / `-t` flag (opt-in):
      additionally emit a flat walk-order `## tree`
      section at the top — a contiguous parent-child
      tree shape with no severity-grouping. Same procs,
      no annotations needed because each parent appears
      directly above its children.
    '''
    flag_tree: bool = False
    pos_args: list = []
    for a in args:
        if a in ('--tree', '-t'):
            flag_tree = True
        else:
            pos_args.append(a)

    if not pos_args:
        print('usage: acli.pytree [--tree|-t] <pid|pgrep-pattern> [...]')
        return 1
    if psutil is None:
        print('pytree requires psutil; install via `uv pip install psutil`')
        return 1

    roots: list = []
    for a in pos_args:
        roots.extend(_resolve_pids(a))
    roots = sorted(set(roots))
    if not roots:
        print(f'(no procs match: {pos_args})')
        return 1

    # statuses considered "defunct" — STATUS_ZOMBIE is the
    # common case (`Z`); STATUS_DEAD (`X`) is rarer but kernel-
    # reported and equally not-coming-back.
    defunct_statuses: set = {
        psutil.STATUS_ZOMBIE,
        getattr(psutil, 'STATUS_DEAD', 'dead'),
    }

    seen: set = set()
    walk_order: list = []  # [(proc, depth)] preserved walk order
    live: list = []        # [(proc, depth)]
    orphans: list = []
    zombies: list = []
    gone: list = []

    # parent-bucket lookup populated post-classification so
    # `_row()` can annotate cross-bucket parent refs.
    pid_to_bucket: dict = {}

    for r in roots:
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
            # severity order: zombie > orphan > live.
            if status in defunct_statuses:
                zombies.append(entry)
                pid_to_bucket[p.pid] = 'zombies'
            elif ppid == 1:
                orphans.append(entry)
                pid_to_bucket[p.pid] = 'orphans'
            else:
                live.append(entry)
                pid_to_bucket[p.pid] = 'live'
            walk_order.append(entry)

    total: int = len(live) + len(orphans) + len(zombies)
    print(f'# pytree: {total} procs across roots {roots}')

    hdr = '  ' + 'PID'.rjust(7) + '  ' + 'PPID'.rjust(7) + '  '
    hdr += 'STATUS'.ljust(10) + '  CMD'

    def _row(entry, bucket: str|None = None):
        '''
        Render `(proc, depth)` as an aligned row. Tree depth is
        rendered as a `└─` marker on the CMD column so PID/PPID/
        STATUS stay column-aligned.

        When `bucket` is given AND the row's parent lives in a
        *different* bucket, append a `[parent: <pid> (in `<b>`)]`
        suffix so the `└─` marker can be resolved across the
        severity-section split.
        '''
        p, depth = entry
        tree_pfx = ('   ' * depth) + ('└─ ' if depth > 0 else '')

        # cross-bucket parent annotation; safe to compute up
        # front because `p.ppid()` is cheap and rarely
        # raises (parent pid is read from `/proc/<pid>/stat`,
        # cached by psutil).
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
                parent_bucket: str|None = pid_to_bucket.get(parent_pid)
                if (
                    parent_bucket is not None
                    and parent_bucket != bucket
                ):
                    parent_anno = (
                        f'  [parent: {parent_pid} '
                        f'(in `{parent_bucket}`)]'
                    )

        # NOTE: `psutil.ZombieProcess` is a *subclass* of
        # `psutil.NoSuchProcess`, but the proc is NOT gone —
        # it's a zombie whose `/proc/<pid>/cmdline` is empty/
        # unreadable. Catch it FIRST so we still render a
        # row (using fields that DO work on zombies: pid,
        # ppid, status, name).
        try:
            cmd = ' '.join(p.cmdline())[:140] or '[' + p.name() + ']'
            r = '  ' + str(p.pid).rjust(7)
            r += '  ' + str(p.ppid()).rjust(7)
            r += '  ' + p.status().ljust(10)
            r += '  ' + tree_pfx + cmd + parent_anno
            return r
        except psutil.ZombieProcess:
            try:
                ppid_str = str(p.ppid())
                name = p.name()
            except psutil.NoSuchProcess:
                ppid_str, name = '?', '?'
            r = '  ' + str(p.pid).rjust(7)
            r += '  ' + ppid_str.rjust(7)
            r += '  ' + 'zombie'.ljust(10)
            r += '  ' + tree_pfx + '[' + name + ' <defunct>]' + parent_anno
            return r
        except psutil.NoSuchProcess:
            return '  ' + str(p.pid).rjust(7) + '  (gone mid-walk)'

    def _section(
        title: str,
        procs: list,
        hint: str = '',
        bucket: str|None = None,
    ):
        print(f'\n## {title} ({len(procs)})' + (f'  — {hint}' if hint else ''))
        if not procs:
            print('  (none)')
            return
        print(hdr)
        for p in procs:
            print(_row(p, bucket=bucket))

    # `--tree` opt-in: emit a flat walk-order section first
    # so the parent-child tree shape is contiguous (no
    # severity-grouping). No `bucket` arg → no cross-bucket
    # annotation, since each parent appears directly above
    # its children here.
    if flag_tree:
        _section(
            'tree', walk_order,
            'flat walk-order, parent-child preserved',
        )

    # severity-ordered: most concerning first. Each section
    # passes its own `bucket` name so `_row()` can annotate
    # rows whose parents live in a different section.
    _section(
        'zombies', zombies,
        'status `Z`/`X`, parent has not reaped',
        bucket='zombies',
    )
    _section(
        'orphans', orphans,
        '`ppid==1`, reparented to init (leaked / parent gone)',
        bucket='orphans',
    )
    _section('live', live, bucket='live')

    if gone:
        print(f'\n## gone-during-walk ({len(gone)}): {gone}')


# --- hung-dump ------------------------------------------------

def _hung_dump(args):
    '''
    kernel + python state for a hung pytest/tractor tree.
    walks all descendants of each `<pid|pgrep-pat>` arg.

    usage: acli.hung_dump <pid|pgrep-pattern> [...]

    note: `/proc/<pid>/stack` and `py-spy dump` typically
    require CAP_SYS_PTRACE — invoked via `sudo -n`. run
    `sudo true` first to cache creds.
    '''
    if not args:
        print('usage: acli.hung_dump <pid|pgrep-pattern> [...]')
        return 1

    # cache sudo creds upfront so per-pid `sudo -n` calls
    # for `cat /proc/<pid>/stack` and `py-spy dump` don't
    # each prompt (or silently fail).
    have_sudo: bool = _ensure_sudo_cached()

    roots: list = []
    for a in args:
        roots.extend(_resolve_pids(a))
    roots = sorted(set(roots))
    if not roots:
        print(f'(no procs match: {args})')
        return 1

    pids: list = []
    seen: set = set()
    for r in roots:
        if psutil is not None:
            walk = [p.pid for p in _walk_tree_psutil(r)]
        else:
            walk = _walk_tree_pgrep(r)
        for pid in walk:
            if pid not in seen:
                seen.add(pid)
                pids.append(pid)

    print(f'# tree: {pids}')
    print('\n## ps forest')
    $[ps -o pid,ppid,pgid,stat,cmd -p @(','.join(map(str, pids)))]

    for pid in pids:
        print(f'\n## pid {pid}')

        for f in ('wchan', 'stack'):
            path = Path(f'/proc/{pid}/{f}')
            try:
                txt = path.read_text().rstrip()
                print(f'-- /proc/{pid}/{f} --\n{txt}')
            except PermissionError:
                if not have_sudo:
                    print(
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
                    print(f'-- /proc/{pid}/{f} (sudo) --\n{txt}')
                except sp.CalledProcessError:
                    print(
                        f'-- /proc/{pid}/{f}: '
                        'sudo cred expired? rerun --'
                    )
            except FileNotFoundError:
                print(f'-- /proc/{pid}/{f}: proc gone --')

        print(f'-- py-spy {pid} --')
        if not have_sudo:
            print('  (skipped — no sudo)')
            continue
        try:
            $[sudo -n py-spy dump --pid @(pid) --locals]
        except Exception as e:
            print(f'  (py-spy failed: {e})')


# --- bindspace-scan -------------------------------------------

def _bindspace_scan(args):
    '''
    Scan a tractor UDS bindspace dir for orphan sock files
    (those whose embedded `<pid>` no longer corresponds to
    a live process).

    usage: acli.bindspace_scan [<dir>]
    default: `$XDG_RUNTIME_DIR/tractor`
             (or `/run/user/<uid>/tractor`)
    '''
    if args:
        bs_dir = Path(args[0])
    else:
        runtime = os.environ.get(
            'XDG_RUNTIME_DIR',
            f'/run/user/{os.getuid()}',
        )
        bs_dir = Path(runtime) / 'tractor'

    if not bs_dir.exists():
        print(f'(no bindspace at {bs_dir})')
        return 1

    socks = sorted(bs_dir.glob('*.sock'))
    print(f'## bindspace {bs_dir} ({len(socks)} sock file(s))')

    live: list = []
    orphans: list = []
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
            live.append((s, pid, name))
        except ProcessLookupError:
            orphans.append((s, pid, name))
        except PermissionError:
            # exists but owned by another user
            live.append((s, pid, name))

    print(f'\n## live ({len(live)})')
    if not live:
        print('  (none)')
    for s, pid, name in live:
        row = '  ' + str(pid).rjust(7)
        row += '  ' + name.ljust(32)
        row += '  ' + s.name
        print(row)

    print(f'\n## orphaned ({len(orphans)})')
    if not orphans:
        print('  (none)')
    for s, pid, name in orphans:
        row = '  ' + str(pid).rjust(7)
        row += '  ' + name.ljust(32)
        row += '  ' + s.name + '  (no live proc)'
        print(row)

    if bogus:
        print(f'\n## unparseable ({len(bogus)})')
        for s in bogus:
            print(f'  {s.name}')

    if orphans:
        unlink_cmd = ' '.join(str(o[0]) for o in orphans)
        print(f'\nto unlink orphans:\n  rm {unlink_cmd}')


# --- acli.reap ------------------------------------------------

def _tractor_reap(args):
    '''
    SC-polite zombie-subactor reaper + optional `/dev/shm/`
    orphan-segment sweep + optional UDS sock-file sweep.

    usage: acli.reap [-h] [--parent PID] [--grace SEC]
                    [--dry-run] [--shm | --shm-only]
                    [--uds | --uds-only]

    phases (run in order when enabled):

      1. process reap — finds tractor subactor procs left
         alive after a `pytest`/app run that failed to fully
         cancel its tree. Default = orphan-mode (PPid==1
         init-reparented procs whose cwd matches repo root
         AND cmdline contains `python`). With `--parent`,
         scopes to descendants of a specific live PID.
         SIGINT first, then SIGKILL after `--grace` (default
         3.0s).
      2. shm sweep (`--shm`/`--shm-only`) — unlinks
         `/dev/shm/<file>` entries owned by the current uid
         that no live process has open. Needed because
         `tractor` disables `mp.resource_tracker`.
      3. UDS sweep (`--uds`/`--uds-only`) — unlinks
         `${XDG_RUNTIME_DIR}/tractor/<name>@<pid>.sock`
         files whose binder pid is dead (or the `1616`
         registry sentinel). See issue #452.

    Mirrors `scripts/tractor-reap` (use `-n`/`--dry-run`
    first to see what would be touched).

    '''
    import argparse

    parser = argparse.ArgumentParser(
        prog='acli.reap',
        description=_tractor_reap.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--parent', '-p',
        type=int,
        default=None,
        help='descendant-mode: reap procs with PPid==<pid>',
    )
    parser.add_argument(
        '--grace', '-g',
        type=float,
        default=3.0,
        help='SIGINT grace window in seconds (default 3.0)',
    )
    parser.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='list matched pids/paths but do not signal/unlink',
    )
    parser.add_argument(
        '--shm',
        action='store_true',
        help='also unlink orphaned /dev/shm segments',
    )
    parser.add_argument(
        '--shm-only',
        action='store_true',
        help='skip process reap; only do the shm sweep',
    )
    parser.add_argument(
        '--uds',
        action='store_true',
        help='also unlink orphaned UDS sock-files',
    )
    parser.add_argument(
        '--uds-only',
        action='store_true',
        help='skip process reap + shm; only do the UDS sweep',
    )

    try:
        ns = parser.parse_args(args)
    except SystemExit as se:
        # `argparse` raises SystemExit on `-h`/bad-args; let
        # xonsh treat it as a normal alias return code.
        return int(se.code) if se.code is not None else 0

    skip_proc_reap: bool = (
        ns.shm_only
        or
        ns.uds_only
    )

    # repo-root resolution: `git rev-parse --show-toplevel`
    # first, falling back to the xontrib file's parent of
    # parent. mirrors `scripts/tractor-reap._repo_root()`.
    try:
        repo_str: str = sp.check_output(
            ['git', 'rev-parse', '--show-toplevel'],
            stderr=sp.DEVNULL,
            text=True,
        ).strip()
        repo: Path = Path(repo_str)
    except (sp.CalledProcessError, FileNotFoundError):
        repo: Path = Path(__file__).resolve().parent.parent

    # lazy-import the reap helpers since the package may not
    # have been on `sys.path` at xontrib-load time (e.g. the
    # contrib was sourced before activating the venv).
    import sys
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from tractor._testing._reap import (
        find_descendants,
        find_orphans,
        find_orphaned_shm,
        find_orphaned_uds,
        reap,
        reap_shm,
        reap_uds,
    )

    rc: int = 0

    # phase 1: process reap (skipped under `--*-only`)
    if not skip_proc_reap:
        if ns.parent is not None:
            pids: list = find_descendants(ns.parent)
            mode: str = f'descendants of PPid={ns.parent}'
        else:
            pids = find_orphans(repo)
            mode = f'orphans (PPid=1, cwd={repo})'

        if not pids:
            print(f'[acli.reap] no {mode} to reap')
        elif ns.dry_run:
            print(
                f'[acli.reap] dry-run — {mode}:\n  {pids}'
            )
        else:
            _, survivors = reap(pids, grace=ns.grace)
            if survivors:
                rc = 1

    # phase 2: shm sweep (opt-in)
    if ns.shm or ns.shm_only:
        leaked: list = find_orphaned_shm()
        if not leaked:
            print(
                '[acli.reap] no orphaned /dev/shm '
                'segments to sweep'
            )
        elif ns.dry_run:
            print(
                f'[acli.reap] dry-run — {len(leaked)} '
                f'orphaned shm segment(s):\n  {leaked}'
            )
        else:
            _, errors = reap_shm(leaked)
            if errors:
                rc = 1

    # phase 3: UDS sweep (opt-in)
    if ns.uds or ns.uds_only:
        leaked_uds: list = find_orphaned_uds()
        if not leaked_uds:
            print(
                '[acli.reap] no orphaned UDS sock-files '
                'to sweep'
            )
        elif ns.dry_run:
            print(
                f'[acli.reap] dry-run — {len(leaked_uds)} '
                f'orphaned UDS sock-file(s):\n  {leaked_uds}'
            )
        else:
            _, errors = reap_uds(leaked_uds)
            if errors:
                rc = 1

    return rc


# --- registration ---------------------------------------------

# all aliases under the `acli.` namespace so xonsh's prefix-
# completion makes them feel like a sub-cmd group: type
# `acli.<TAB>` and the full set is suggested. no parent
# `acli` cmd exists — the dot is purely a naming convention.
_TCLI_ALIASES: dict = {
    'acli.pytree': _pytree,
    'acli.hung_dump': _hung_dump,
    'acli.bindspace_scan': _bindspace_scan,
    'acli.reap': _tractor_reap,
}

for _name, _fn in _TCLI_ALIASES.items():
    aliases[_name] = _fn


# xontrib protocol hooks (for `xontrib load tractor_diag`).
# also harmless when sourced directly.
def _load_xontrib_(xsh, **_):
    return {}


def _unload_xontrib_(xsh, **_):
    for name in _TCLI_ALIASES:
        aliases.pop(name, None)
    return {}
