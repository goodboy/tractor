'''
Tests for `tractor.devx._proctitle` (per-actor `setproctitle`)
and the intrinsic-signal sub-actor detection in
`tractor._testing._reap`.

The proctitle is set in `tractor._child._actor_child_main()`
after `Actor` construction, so any spawned sub-actor process
should:

  - have `argv[0]` (== `/proc/<pid>/cmdline`) start with
    `tractor[<aid.reprol()>]`
  - have `/proc/<pid>/comm` start with `tractor[` (kernel
    truncates to ~15 bytes)
  - be detected as a tractor sub-actor by
    `_is_tractor_subactor(pid)` via the cmdline marker.

`set_actor_proctitle()` itself is also unit-tested in-process
to verify the format string.

'''
from __future__ import annotations
import platform

import psutil
import pytest
import trio
import tractor

from tractor.runtime._runtime import Actor
from tractor.devx._proctitle import set_actor_proctitle
from tractor._testing._reap import (
    _is_tractor_subactor,
    _read_cmdline,
    _read_comm,
)


_non_linux: bool = platform.system() != 'Linux'


def test_set_actor_proctitle_format():
    '''
    `set_actor_proctitle()` returns the canonical
    `tractor[<aid.reprol()>]` form and actually mutates
    the running proc's title.

    '''
    pytest.importorskip(
        'setproctitle',
        reason='`setproctitle` is an optional runtime dep',
    )
    import setproctitle

    # save + restore so we don't pollute pytest's own title
    saved: str = setproctitle.getproctitle()
    try:
        actor = Actor(
            name='unit_test_actor',
            uuid='1027301b-a0e3-430e-8806-a5279f21abe6',
        )
        title: str = set_actor_proctitle(actor)

        # canonical wrapping: `tractor[<aid.reprol()>]`. We
        # compare against the runtime-computed `reprol()`
        # rather than a hard-coded value so the test stays
        # decoupled from `Aid.reprol()`'s internal format
        # (currently `<name>@<pid>`, but could evolve).
        expected: str = f'tractor[{actor.aid.reprol()}]'
        assert title == expected
        # sanity: the actor's name must be in the title
        # somewhere (so a future `reprol()` change that
        # drops the name is also caught).
        assert 'unit_test_actor' in title

        # actually set on the running proc
        assert setproctitle.getproctitle() == title

    finally:
        setproctitle.setproctitle(saved)


@pytest.mark.skipif(
    _non_linux,
    reason=(
        'detection helpers read `/proc/<pid>/{cmdline,comm}` '
        'which is Linux-specific'
    ),
)
def test_subactor_proctitle_visible_via_proc():
    '''
    Spawn a sub-actor and verify its proc-title is visible
    via both `/proc/<pid>/cmdline` AND `/proc/<pid>/comm`,
    AND that `_is_tractor_subactor()` correctly identifies
    it.

    '''
    pytest.importorskip('setproctitle')

    async def main() -> dict:
        async with tractor.open_nursery() as an:
            portal = await an.start_actor('proctitle_boi')
            # let the child finish setproctitle in
            # `_actor_child_main`
            await trio.sleep(0.3)

            # the sub-actor's pid is on the portal's chan
            # repr; psutil-walk `me.children()` is simpler.
            me = psutil.Process()
            sub_pids: list[int] = [
                p.pid for p in me.children(recursive=True)
            ]
            assert sub_pids, (
                'expected at least one spawned sub-actor pid'
            )

            results: dict = {}
            for pid in sub_pids:
                results[pid] = {
                    'cmdline': _read_cmdline(pid),
                    'comm': _read_comm(pid),
                    'is_tractor': _is_tractor_subactor(pid),
                }

            await portal.cancel_actor()
            return results

    found: dict = trio.run(main)

    # at least one of the spawned procs should match the
    # `proctitle_boi` actor we started; assert the proc-
    # title shape on it specifically.
    matched: list[tuple[int, dict]] = [
        (pid, info)
        for pid, info in found.items()
        if 'proctitle_boi' in info['cmdline']
    ]
    assert matched, (
        f'no sub-actor pid had a `proctitle_boi` cmdline; '
        f'all={found}'
    )

    pid, info = matched[0]
    # canonical proctitle prefix in cmdline (full form)
    assert info['cmdline'].startswith('tractor[proctitle_boi@'), (
        f'cmdline missing `tractor[proctitle_boi@…]` prefix: '
        f'{info["cmdline"]!r}'
    )
    # comm is kernel-truncated to ~15 bytes — just check the
    # `tractor[` prefix made it.
    assert info['comm'].startswith('tractor['), (
        f'comm missing `tractor[` prefix: {info["comm"]!r}'
    )
    # intrinsic-signal detector should match.
    assert info['is_tractor'] is True


@pytest.mark.skipif(
    _non_linux,
    reason='reads /proc/<pid>/{cmdline,comm}',
)
def test_is_tractor_subactor_negative():
    '''
    `_is_tractor_subactor()` returns False for non-tractor
    procs (e.g. the pytest test-runner pid itself, which
    is `python -m pytest …` — no `tractor[` proctitle, no
    `tractor._child` cmdline).

    '''
    import os
    assert _is_tractor_subactor(os.getpid()) is False
