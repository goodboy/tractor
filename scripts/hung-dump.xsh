"""
`hung-dump`: snapshot diagnostic state for a hung
`pytest`/`tractor` process tree.

For each pid (and all `pgrep -P` descendants), prints
- `ps` forest header,
- `/proc/<pid>/wchan` + `/proc/<pid>/stack` (kernel-side
  blocked-on syscall),
- `sudo py-spy dump` (python-side stack).

Usage (xonsh):
    source-foreign xonsh ./scripts/hung-dump.xsh
    hung-dump <pid> [<pid> ...]

Or just:
    source ./scripts/hung-dump.xsh

Tip — pipe to a paste buffer:
    hung-dump 1336765 |t /tmp/hung.log
"""

def _hung_dump(args):
    import subprocess as sp
    from pathlib import Path

    if not args:
        print('usage: hung-dump <pid> [<pid> ...]')
        return 1

    def kids(pid):
        try:
            out = sp.check_output(
                ['pgrep', '-P', str(pid)],
                text=True,
            )
        except sp.CalledProcessError:
            return []
        return [int(p) for p in out.split() if p]

    def tree(pid):
        out = [pid]
        for k in kids(pid):
            out.extend(tree(k))
        return out

    pids = [
        p
        for r in (int(a) for a in args)
        for p in tree(r)
    ]

    print(f'# tree: {pids}')
    print('\n## ps forest')
    $[ps -o pid,ppid,pgid,stat,cmd -p @(','.join(map(str, pids)))]

    for p in pids:
        print(f'\n## pid {p}')
        for f in ('wchan', 'stack'):
            path = Path(f'/proc/{p}/{f}')
            try:
                txt = path.read_text().rstrip()
                print(f'-- /proc/{p}/{f} --\n{txt}')
            except PermissionError:
                # `stack` requires CAP_SYS_PTRACE; retry
                # via sudo -n so creds-cached users don't
                # block on prompt.
                try:
                    txt = sp.check_output(
                        ['sudo', '-n', 'cat', str(path)],
                        text=True,
                        stderr=sp.DEVNULL,
                    ).rstrip()
                    print(f'-- /proc/{p}/{f} (via sudo) --\n{txt}')
                except sp.CalledProcessError:
                    print(
                        f'-- /proc/{p}/{f}: '
                        'PermissionError (try `sudo true` first) --'
                    )
            except FileNotFoundError:
                print(f'-- /proc/{p}/{f}: proc gone --')

        print(f'-- py-spy {p} --')
        try:
            $[sudo -n py-spy dump --pid @(p)]
        except Exception as e:
            print(f'  (py-spy failed: {e})')


aliases['hung-dump'] = _hung_dump
