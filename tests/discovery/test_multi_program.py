"""
Multiple python programs invoking the runtime.
"""
from __future__ import annotations
import platform
import subprocess
import time
from typing import (
    TYPE_CHECKING,
)

import pytest
import trio
import tractor
from tractor._testing import (
    tractor_test,
)
from tractor import (
    current_actor,
    Actor,
    Context,
    Portal,
)
from tractor.runtime import _state
from ..conftest import (
    sig_prog,
    _INT_SIGNAL,
    _INT_RETURN_CODE,
)

if TYPE_CHECKING:
    from tractor.msg import Aid
    from tractor.discovery._addr import (
        UnwrappedAddress,
    )


_non_linux: bool = platform.system() != 'Linux'


# NOTE, multi-program tests historically triggered both
# UDS sock-file leaks (daemon-subproc SIGKILL paths) AND
# trio `WakeupSocketpair.drain()` busy-loops
# (`test_register_duplicate_name`). Track + detect
# per-test as a regression net.
pytestmark = pytest.mark.usefixtures(
    'track_orphaned_uds_per_test',
    'detect_runaway_subactors_per_test',
)


def test_abort_on_sigint(
    daemon: subprocess.Popen,
):
    assert daemon.returncode is None
    time.sleep(0.1)
    sig_prog(daemon, _INT_SIGNAL)
    assert daemon.returncode == _INT_RETURN_CODE

    # XXX: oddly, couldn't get capfd.readouterr() to work here?
    if platform.system() != 'Windows':
        # don't check stderr on windows as its empty when sending CTRL_C_EVENT
        assert "KeyboardInterrupt" in str(daemon.stderr.read())


@tractor_test
async def test_cancel_remote_registrar(
    daemon: subprocess.Popen,
    reg_addr: UnwrappedAddress,
):
    assert not current_actor().is_registrar
    async with tractor.get_registry(reg_addr) as portal:
        await portal.cancel_actor()

    time.sleep(0.1)
    # the registrar channel server is cancelled but not its main task
    assert daemon.returncode is None

    # no registrar socket should exist
    with pytest.raises(OSError):
        async with tractor.get_registry(reg_addr) as portal:
            pass


def test_register_duplicate_name(
    daemon: subprocess.Popen,
    reg_addr: UnwrappedAddress,
):
    # bug-class-3 breadcrumbs: the *last* `[CANCEL]` line that
    # appears under `--ll cancel`/`TRACTOR_LOG_FILE=...` names the
    # cancel-cascade boundary that's parked. Pair with
    # `_trio_main` entry/exit breadcrumbs in
    # `tractor/spawn/_entry.py` to triangulate the swallow point.
    log = tractor.log.get_logger('tractor.tests.test_multi_program')

    async def main():
        log.cancel('test_register_duplicate_name: enter `main()`')
        try:
            async with tractor.open_nursery(
                registry_addrs=[reg_addr],
            ) as an:
                log.cancel(
                    'test_register_duplicate_name: '
                    'actor nursery opened'
                )

                assert not current_actor().is_registrar

                p1 = await an.start_actor('doggy')
                log.cancel(
                    'test_register_duplicate_name: '
                    'spawned doggy #1'
                )
                p2 = await an.start_actor('doggy')
                log.cancel(
                    'test_register_duplicate_name: '
                    'spawned doggy #2'
                )

                async with tractor.wait_for_actor('doggy') as portal:
                    log.cancel(
                        'test_register_duplicate_name: '
                        '`wait_for_actor` returned'
                    )
                    assert portal.channel.uid in (p2.channel.uid, p1.channel.uid)

                log.cancel(
                    'test_register_duplicate_name: '
                    'ABOUT TO CALL `an.cancel()`'
                )
                await an.cancel()
                log.cancel(
                    'test_register_duplicate_name: '
                    '`an.cancel()` returned'
                )
        finally:
            log.cancel(
                'test_register_duplicate_name: '
                '`open_nursery.__aexit__` returned, leaving `main()`'
            )

    # XXX, run manually since we want to start this root **after**
    # the other "daemon" program with it's own root.
    trio.run(main)


@pytest.mark.parametrize(
    'n_dups',
    [
        2,
        # `n_dups=4` exposes a SEPARATE pre-existing race: under
        # rapid same-name spawning against a forkserver +
        # registrar, ONE of the spawned doggies (typically the
        # 3rd) `sys.exit(2)`s during boot before completing
        # parent-handshake. Surfaces now (post the spawn-time
        # `wait_for_peer_or_proc_death` fix) as `ActorFailure
        # rc=2`; previously it was silently masked by the
        # handshake-wait parking forever.
        #
        # Tracked separately in,
        # https://github.com/goodboy/tractor/issues/456 
        pytest.param(
            4,
            marks=pytest.mark.xfail(
                strict=False,
                reason=(
                    'doggy boot-race rc=2 under rapid same-name '
                    'spawn — separate bug from cancel-cascade'
                ),
            ),
        ),
        8,
    ],
    ids=lambda n: f'n_dups={n}',
)
def test_dup_name_cancel_cascade_escalates_to_hard_kill(
    daemon: subprocess.Popen,
    reg_addr: UnwrappedAddress,
    n_dups: int,
):
    '''
    Regression for the duplicate-name cancel-cascade hang under
    `tcp+main_thread_forkserver`.

    When N actors share a single name and the parent calls
    `an.cancel()`, the daemon registrar gets N `register_actor` RPCs
    in tight succession. Under TCP+MTF, kernel-level socket-buffer
    contention can push at least one sub-actor's cancel-RPC ack past
    `Portal.cancel_timeout` (default 0.5s).

    Pre-fix, `Portal.cancel_actor()` silently returned `False` on
    that timeout, the supervisor's outer `move_on_after(3)` never
    fired (each per-portal task always returned ≤0.5s, never
    exceeded 3s), and `soft_kill()`'s `await wait_func(proc)` parked
    forever — deadlocking nursery `__aexit__`.

    Post-fix, `Portal.cancel_actor()` raises `ActorTooSlowError` on
    the bounded-wait timeout, and `ActorNursery.cancel()`'s
    per-child wrapper escalates to `proc.terminate()` (hard-kill).
    The full nursery teardown therefore stays bounded even under
    pathological timing.

    `n_dups` is parametrized to widen the race window — more
    same-name siblings = more concurrent register-RPCs at the
    daemon = higher probability of hitting the contention path.

    '''
    log = tractor.log.get_logger(
        'tractor.tests.test_multi_program'
    )

    # outer hard ceiling: a regression should fail-fast, NOT hang
    # the test session for minutes. Budget scales with `n_dups`
    # since each extra same-name sibling adds ~spawn-cost +
    # potential cancel-ack-timeout escalation latency under
    # TCP+forkserver. ~5s/sibling + 15s baseline gives plenty of
    # headroom while still failing-loud on a real hang.
    fail_after_s: int = 15 + (5 * n_dups)

    async def main():
        log.cancel(
            f'enter `main()` n_dups={n_dups}'
        )
        with trio.fail_after(fail_after_s):
            async with tractor.open_nursery(
                registry_addrs=[reg_addr],
            ) as an:
                portals: list[Portal] = []
                for i in range(n_dups):
                    p: Portal = await an.start_actor('doggy')
                    portals.append(p)
                    log.cancel(
                        f'spawned doggy #{i + 1}/{n_dups}'
                    )

                # at least one of the N must be discoverable by
                # name; doesn't matter which one (registrar will
                # have last-wins semantics under same-name).
                async with tractor.wait_for_actor('doggy') as portal:
                    expected_uids = {p.channel.uid for p in portals}
                    assert portal.channel.uid in expected_uids

                # critical section: this MUST return within
                # `fail_after_s` even when one or more cancel-RPC
                # acks time out. Pre-fix, this hangs forever.
                log.cancel('about to call `an.cancel()`')
                await an.cancel()
                log.cancel('`an.cancel()` returned')

        # post-teardown sanity: every child proc must be reaped.
        # If escalation worked, even timed-out cancel-RPCs would
        # have triggered `proc.terminate()` and the procs are dead.
        for p in portals:
            # `Portal.channel.connected()` -> False once the
            # underlying chan disconnected (clean exit OR
            # hard-killed proc both produce disconnect).
            assert not p.channel.connected(), (
                f'Portal chan still connected post-teardown?\n'
                f'{p.channel}'
            )

    trio.run(main)


@tractor.context
async def get_root_portal(
    ctx: Context,
):
    '''
    Connect back to the root actor manually (using `._discovery` API)
    and ensure it's contact info is the same as our immediate parent.

    '''
    sub: Actor = current_actor()
    rtvs: dict = _state._runtime_vars
    raddrs: list[UnwrappedAddress] = rtvs['_root_addrs']

    # await tractor.pause()
    # XXX, in case the sub->root discovery breaks you might need
    # this (i know i did Xp)!!
    # from tractor.devx import mk_pdb
    # mk_pdb().set_trace()

    assert (
        len(raddrs) == 1
        and
        list(sub._parent_chan.raddr.unwrap()) in raddrs
    )

    # connect back to our immediate parent which should also
    # be the actor-tree's root.
    from tractor.discovery._api import get_root
    ptl: Portal
    async with get_root() as ptl:
        root_aid: Aid = ptl.chan.aid
        parent_ptl: Portal = current_actor().get_parent()
        assert (
            root_aid.name == 'root'
            and
            parent_ptl.chan.aid == root_aid
        )
        await ctx.started()


def test_non_registrar_spawns_child(
    daemon: subprocess.Popen,
    reg_addr: UnwrappedAddress,
    loglevel: str,
    debug_mode: bool,
    ci_env: bool,
):
    '''
    Ensure a non-regristar (serving) root actor can spawn a sub and
    that sub can connect back (manually) to it's rent that is the
    root without issue.

    More or less this audits the global contact info in
    `._state._runtime_vars`.

    '''
    async def main():

        # XXX, since apparently on macos in GH's CI it can be a race
        # with the `daemon` registrar on grabbing the socket-addr..
        if ci_env and _non_linux:
            await trio.sleep(.5)

        async with tractor.open_nursery(
            registry_addrs=[reg_addr],
            loglevel=loglevel,
            debug_mode=debug_mode,
        ) as an:

            actor: Actor = tractor.current_actor()
            assert not actor.is_registrar
            sub_ptl: Portal = await an.start_actor(
                name='sub',
                enable_modules=[__name__],
            )

            async with sub_ptl.open_context(
                get_root_portal,
            ) as (ctx, _):
                print('Waiting for `sub` to connect back to us..')

            await an.cancel()

    # XXX, run manually since we want to start this root **after**
    # the other "daemon" program with it's own root.
    trio.run(main)
