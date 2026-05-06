"""
`xontrib_tractor_diag`: pytest/tractor diagnostic aliases.

Provides:
  - `pytree <pid|pgrep-pat>`       psutil-backed proc tree,
                                   live + zombies split.
  - `hung-dump <pid|pat> [...]`    kernel `wchan`/`stack` +
                                   `py-spy dump` (incl `--locals`)
                                   for each pid in tree.
  - `bindspace-scan [<dir>]`       find orphaned tractor UDS
                                   sock files (no live owner pid).
                                   default: `$XDG_RUNTIME_DIR/tractor`.

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
        'pytree disabled, hung-dump uses pgrep fallback. '
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

    usage: pytree <pid|pgrep-pattern> [...]

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
    '''
    if not args:
        print('usage: pytree <pid|pgrep-pattern> [...]')
        return 1
    if psutil is None:
        print('pytree requires psutil; install via `uv pip install psutil`')
        return 1

    roots: list = []
    for a in args:
        roots.extend(_resolve_pids(a))
    roots = sorted(set(roots))
    if not roots:
        print(f'(no procs match: {args})')
        return 1

    # statuses considered "defunct" — STATUS_ZOMBIE is the
    # common case (`Z`); STATUS_DEAD (`X`) is rarer but kernel-
    # reported and equally not-coming-back.
    defunct_statuses: set = {
        psutil.STATUS_ZOMBIE,
        getattr(psutil, 'STATUS_DEAD', 'dead'),
    }

    seen: set = set()
    live: list = []     # [(proc, depth)]
    orphans: list = []
    zombies: list = []
    gone: list = []

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
            elif ppid == 1:
                orphans.append(entry)
            else:
                live.append(entry)

    total: int = len(live) + len(orphans) + len(zombies)
    print(f'# pytree: {total} procs across roots {roots}')

    hdr = '  ' + 'PID'.rjust(7) + '  ' + 'PPID'.rjust(7) + '  '
    hdr += 'STATUS'.ljust(10) + '  CMD'

    def _row(entry):
        '''
        Render `(proc, depth)` as an aligned row. Tree depth is
        rendered as a `└─` marker on the CMD column so PID/PPID/
        STATUS stay column-aligned.
        '''
        p, depth = entry
        tree_pfx = ('   ' * depth) + ('└─ ' if depth > 0 else '')
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
            r += '  ' + tree_pfx + cmd
            return r
        except psutil.ZombieProcess:
            try:
                ppid = str(p.ppid())
                name = p.name()
            except psutil.NoSuchProcess:
                ppid, name = '?', '?'
            r = '  ' + str(p.pid).rjust(7)
            r += '  ' + ppid.rjust(7)
            r += '  ' + 'zombie'.ljust(10)
            r += '  ' + tree_pfx + '[' + name + ' <defunct>]'
            return r
        except psutil.NoSuchProcess:
            return '  ' + str(p.pid).rjust(7) + '  (gone mid-walk)'

    def _section(title: str, procs: list, hint: str = ''):
        print(f'\n## {title} ({len(procs)})' + (f'  — {hint}' if hint else ''))
        if not procs:
            print('  (none)')
            return
        print(hdr)
        for p in procs:
            print(_row(p))

    # severity-ordered: most concerning first.
    _section(
        'zombies', zombies,
        'status `Z`/`X`, parent has not reaped',
    )
    _section(
        'orphans', orphans,
        '`ppid==1`, reparented to init (leaked / parent gone)',
    )
    _section('live', live)

    if gone:
        print(f'\n## gone-during-walk ({len(gone)}): {gone}')

    if gone:
        print(f'\n## gone-during-walk ({len(gone)}): {gone}')


# --- hung-dump ------------------------------------------------

def _hung_dump(args):
    '''
    kernel + python state for a hung pytest/tractor tree.
    walks all descendants of each `<pid|pgrep-pat>` arg.

    usage: hung-dump <pid|pgrep-pattern> [...]

    note: `/proc/<pid>/stack` and `py-spy dump` typically
    require CAP_SYS_PTRACE — invoked via `sudo -n`. run
    `sudo true` first to cache creds.
    '''
    if not args:
        print('usage: hung-dump <pid|pgrep-pattern> [...]')
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

    usage: bindspace-scan [<dir>]
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


# --- registration ---------------------------------------------

aliases['pytree'] = _pytree
aliases['hung-dump'] = _hung_dump
aliases['bindspace-scan'] = _bindspace_scan


# xontrib protocol hooks (for `xontrib load tractor_diag`).
# also harmless when sourced directly.
def _load_xontrib_(xsh, **_):
    return {}


def _unload_xontrib_(xsh, **_):
    for name in ('pytree', 'hung-dump', 'bindspace-scan'):
        aliases.pop(name, None)
    return {}
