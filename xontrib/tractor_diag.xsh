"""
`xontrib_tractor_diag`: pytest/tractor diagnostic aliases.

All aliases live under the `acli.` namespace so xonsh's
prefix-completion treats them as a sub-cmd group — type
`acli.<TAB>` to see the full set.

Provides:
  - `acli.ptree <pid|pgrep-pat>`        psutil-backed proc tree,
                                        live + zombies split.
  - `acli.hung_dump <pid|pat> [...]`    kernel `wchan`/`stack` +
                                        `py-spy dump` (incl `--locals`)
                                        for each pid in tree.
  - `acli.bindspace_scan [<name>|<dir>]` find orphaned tractor UDS
                                        sock files (no live owner pid).
                                        bare name -> `$XDG_RUNTIME_DIR/<name>`
                                        (e.g. `piker`, `tractor`);
                                        path -> use as-is.
                                        default: `$XDG_RUNTIME_DIR/tractor`.
  - `acli.dump_all <pid> [--out-dir]    full snapshot bundle —
                          [--label]`    ptree + hung_dump + bindspace
                                        written to a timestamped dir
                                        for sharing / AI introspection.
  - `acli.reap [opts]`                  SC-polite zombie-subactor
                                        reaper + optional `/dev/shm/`
                                        + UDS sock-file sweeps.
                                        alias for `scripts/tractor-reap`.

Loading from repo root:
  xontrib load -p ./xontrib tractor_diag

Or source directly:
  source ./xontrib/tractor_diag.xsh

Pipe-to-paste idiom (xonsh):
  acli.hung_dump pytest |t /tmp/hung.log

The diagnostic core lives in `tractor._testing.trace` so it
can also be invoked from inside pytest tests (e.g. via
`fail_after_w_trace` / `afk_alarm_w_trace` capture-on-hang
helpers) — these aliases are just thin terminal wrappers.

Requires `psutil` for full functionality (`ptree` and the
`hung_dump` tree-walk). Falls back to `pgrep -P` recursion if
missing.
"""

from pathlib import Path

from tractor._testing.trace import (
    dump_all as _dump_all,
    dump_hung_state,
    dump_proc_tree,
    resolve_pids,
    scan_bindspace,
)


# --- ptree ----------------------------------------------------

def _ptree(args):
    '''
    psutil-backed proc tree; per-proc classification into
    severity-ordered buckets so leaked / defunct procs
    don't hide in the noise of normal `live` rows.

    usage: acli.ptree [--tree|-t] <pid|pgrep-pattern> [...]

    See `tractor._testing.trace.dump_proc_tree()` for the
    bucket semantics + classification details.

    As a hot tip, you can use this `xonsh`-script snippet to poll
    a target actor tree:

    .. code-block:: xonsh

        while 1:
            acli.ptree pytest
            @.imp.time.sleep(.3)
            print("\033c", end="")

    '''
    flag_tree: bool = False
    pos_args: list = []
    for a in args:
        if a in ('--tree', '-t'):
            flag_tree = True
        else:
            pos_args.append(a)

    if not pos_args:
        print('usage: acli.ptree [--tree|-t] <pid|pgrep-pattern> [...]')
        return 1

    roots: list = []
    for a in pos_args:
        roots.extend(resolve_pids(a))
    roots = sorted(set(roots))
    if not roots:
        print(f'(no procs match: {pos_args})')
        return 1

    print(dump_proc_tree(roots, flag_tree=flag_tree), end='')


# --- hung-dump -----------------------------------------------

def _hung_dump(args):
    '''
    kernel + python state for a hung pytest/tractor tree.
    walks all descendants of each `<pid|pgrep-pat>` arg.

    usage: acli.hung_dump <pid|pgrep-pattern> [...]

    note: `/proc/<pid>/stack` and `py-spy dump` typically
    require CAP_SYS_PTRACE — invoked via `sudo -n`. If sudo
    isn't cached this alias prompts (via `sudo -v`); for the
    non-interactive equivalent see
    `tractor._testing.trace.dump_hung_state(allow_sudo_prompt=False)`.

    '''
    if not args:
        print('usage: acli.hung_dump <pid|pgrep-pattern> [...]')
        return 1

    roots: list = []
    for a in args:
        roots.extend(resolve_pids(a))
    roots = sorted(set(roots))
    if not roots:
        print(f'(no procs match: {args})')
        return 1

    print(
        dump_hung_state(roots, allow_sudo_prompt=True),
        end='',
    )


# --- bindspace-scan ------------------------------------------

def _bindspace_scan(args):
    '''
    Scan a tractor UDS bindspace dir for orphan sock files.

    usage: acli.bindspace_scan [<name>|<dir>]

    See `tractor._testing.trace.scan_bindspace()` for full arg
    semantics + output-bucket details.

    '''
    arg: str | None = args[0] if args else None
    print(scan_bindspace(arg), end='')


# --- dump-all (snapshot bundle) ------------------------------

def _dump_all_alias(args):
    '''
    Capture a full diag snapshot bundle for a hung proc-tree
    into a timestamped directory for offline / AI inspection.

    usage: acli.dump_all <pid|pgrep-pat>
                        [--label <label>]
                        [--out-dir <path>]

    Writes:
      <out_dir>/<label>__<ts>/{trace.txt, bindspace.txt, meta.json}

    Defaults:
      --label   = `manual`
      --out-dir = `$XDG_CACHE_HOME/tractor/hung-dumps/`
                  (fallback `~/.cache/tractor/hung-dumps/`)

    '''
    import argparse
    parser = argparse.ArgumentParser(
        prog='acli.dump_all',
        description=_dump_all_alias.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        'target',
        help='pid or pgrep -f pattern',
    )
    parser.add_argument(
        '--label', '-l',
        default='manual',
        help='snapshot dir label prefix (default: `manual`)',
    )
    parser.add_argument(
        '--out-dir', '-o',
        type=Path,
        default=None,
        help='snapshot root dir (default: '
             '$XDG_CACHE_HOME/tractor/hung-dumps/)',
    )
    try:
        ns = parser.parse_args(args)
    except SystemExit as se:
        return int(se.code) if se.code is not None else 0

    pids: list = resolve_pids(ns.target)
    if not pids:
        print(f'(no procs match: {ns.target})')
        return 1

    # snapshot scoped to ONE root — pick the first matched
    # pid. Multi-root snapshots can be done by invoking
    # `acli.dump_all <pid>` per root.
    root_pid: int = pids[0]
    if len(pids) > 1:
        print(
            f'[acli.dump_all] {len(pids)} pids matched '
            f'{ns.target!r}; snapshotting tree from {root_pid} '
            f'(re-run per-pid for others: {pids[1:]})'
        )

    dump_dir = _dump_all(
        root_pid,
        out_dir=ns.out_dir,
        label=ns.label,
        allow_sudo_prompt=True,  # CLI: ok to prompt
    )
    print(f'[acli.dump_all] snapshot written to: {dump_dir}')


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

    # `tractor` is assumed to be importable in the xonsh env
    # this xontrib was sourced into (a venv with the package
    # installed). The standalone `scripts/tractor-reap` does
    # `git rev-parse --show-toplevel` + `sys.path.insert` for
    # cold-shell usability — that overhead is unnecessary
    # here since we're already inside the project's venv.
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
            pids = find_orphans()
            mode = (
                'orphans (PPid==1, intrinsic '
                'cmdline/comm match — `tractor[…]` or '
                '`tractor._child`)'
            )

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
    'acli.ptree': _ptree,
    'acli.hung_dump': _hung_dump,
    'acli.bindspace_scan': _bindspace_scan,
    'acli.dump_all': _dump_all_alias,
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
