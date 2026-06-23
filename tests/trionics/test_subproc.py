'''
Unit tests for `tractor.trionics.supervise_run_process` (in
`tractor.trionics._subproc`) and its per-line std-stream relay.

Hermetic `trio`-only coverage (no actor-runtime needed):

- per-line stdout relay -> `log.io`
- parent controlling-tty isolation (child fd1 is a pipe, fd0
  `/dev/null` — never the parent `/dev/pts/*`)
- mandatory concurrent pipe-drain (no deadlock on >64KiB
  no-newline output)
- live stderr relay + `CalledProcessError` rebuild (rc!=0 note)
- legacy capture-stderr CPE note path

'''
from functools import partial
import platform
import subprocess

import pytest
import trio

from tractor.trionics import (
    _subproc,
    collapse_eg,
    supervise_run_process,
)


def _capture_relay(monkeypatch, level: str = 'io') -> list[str]:
    '''
    Redirect `_subproc.log.<level>` (the relay's emit method —
    `io` by default, see `supervise_run_process(relay_level=...)`)
    into a list so tests can assert on the relayed lines.

    '''
    records: list[str] = []
    monkeypatch.setattr(
        _subproc.log,
        level,
        lambda msg, *a, **k: records.append(msg),
    )
    return records


def test_stdout_relayed_per_line(monkeypatch):
    records = _capture_relay(monkeypatch)

    cmd = [
        'sh', '-c',
        'for i in 1 2 3; do echo line=$i; done',
    ]

    async def main():
        async with trio.open_nursery() as tn:
            await tn.start(
                partial(
                    supervise_run_process,
                    cmd,
                    label='t-out',
                    relay_stdout=True,
                )
            )

    trio.run(main)

    out_lines = [r for r in records if '[t-out:out]' in r]
    assert any('line=1' in r for r in out_lines)
    assert any('line=2' in r for r in out_lines)
    assert any('line=3' in r for r in out_lines)


@pytest.mark.skipif(
    platform.system() != 'Linux',
    reason='reads `/proc/self/fd` — Linux-only',
)
def test_parent_tty_isolated(monkeypatch):
    records = _capture_relay(monkeypatch)

    cmd = [
        'sh', '-c',
        'readlink /proc/self/fd/0; readlink /proc/self/fd/1',
    ]

    async def main():
        async with trio.open_nursery() as tn:
            await tn.start(
                partial(
                    supervise_run_process,
                    cmd,
                    label='t-tty',
                    relay_stdout=True,
                )
            )

    trio.run(main)

    relayed = '\n'.join(records)
    # fd1 (stdout) must be OUR pipe, never a controlling tty.
    assert 'pipe:' in relayed
    assert '/dev/pts/' not in relayed
    # fd0 (stdin) is pinned to DEVNULL.
    assert '/dev/null' in relayed


def test_no_deadlock_on_big_unnewlined_output(monkeypatch):
    '''
    >64KiB of output with NO newline: only completes because the
    relay reader concurrently drains the pipe (else the child
    blocks on `write()` when the OS pipe buffer fills).

    '''
    records = _capture_relay(monkeypatch)

    cmd = [
        'sh', '-c',
        'head -c 200000 /dev/zero | tr "\\0" x',
    ]

    async def main():
        # generous vs the ~ms real runtime, but bounded so a
        # genuine pipe-fill deadlock fails fast.
        with trio.fail_after(2):
            async with trio.open_nursery() as tn:
                await tn.start(
                    partial(
                        supervise_run_process,
                        cmd,
                        label='t-big',
                        relay_stdout=True,
                    )
                )

    trio.run(main)

    big = ''.join(
        r.split('] ', 1)[-1]
        for r in records
        if '[t-big:out]' in r
    )
    assert len(big) == 200_000


def test_stderr_relay_and_cpe_rebuild(monkeypatch):
    '''
    `relay_stderr=True` PIPEs stderr ourselves (mutually
    exclusive with trio's `capture_stderr`), so on rc!=0 the
    wrapper rebuilds a `CalledProcessError` from the live
    accumulator and `.add_note()`s its `.stderr` — AND the
    stderr is relayed per-line live.

    '''
    records = _capture_relay(monkeypatch)

    cmd = [
        'sh', '-c',
        'echo boom 1>&2; exit 3',
    ]

    async def main():
        # `collapse_eg()` unwraps the parent-nursery's single-exc
        # eg so the bare CPE bubbles straight out (mirrors real
        # caller usage).
        async with (
            collapse_eg(),
            trio.open_nursery() as tn,
        ):
            await tn.start(
                partial(
                    supervise_run_process,
                    cmd,
                    label='t-err',
                    relay_stderr=True,
                    check=True,
                )
            )

    with pytest.raises(subprocess.CalledProcessError) as ei:
        trio.run(main)

    cpe = ei.value
    assert cpe.returncode == 3
    # rebuilt `.stderr` (trio did NOT capture since we PIPE'd it).
    assert b'boom' in (cpe.stderr or b'')
    # note attached for legible teardown reporting.
    assert any(
        'boom' in n
        for n in getattr(cpe, '__notes__', [])
    )
    # AND it was relayed live per-line.
    assert any(
        '[t-err:err]' in r and 'boom' in r
        for r in records
    )


def test_nonrelay_cpe_note(monkeypatch):
    '''
    No live relay: stderr is silently drained + captured (NOT
    emitted), and on rc!=0 the wrapper rebuilds the
    `CalledProcessError` from that accumulator with a `.stderr`
    note — same deterministic post-drain path as the relay case.

    '''
    cmd = [
        'sh', '-c',
        'echo nope 1>&2; exit 7',
    ]

    async def main():
        async with (
            collapse_eg(),
            trio.open_nursery() as tn,
        ):
            await tn.start(
                partial(
                    supervise_run_process,
                    cmd,
                    label='t-legacy',
                    check=True,
                    # relay_* default False -> silent
                    # drain+capture for the CPE note.
                )
            )

    with pytest.raises(subprocess.CalledProcessError) as ei:
        trio.run(main)

    cpe = ei.value
    assert cpe.returncode == 7
    assert b'nope' in (cpe.stderr or b'')
    assert any(
        'nope' in n
        for n in getattr(cpe, '__notes__', [])
    )
