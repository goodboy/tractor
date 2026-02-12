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
    _state,
    Actor,
    Context,
    Portal,
)
from .conftest import (
    sig_prog,
    _INT_SIGNAL,
    _INT_RETURN_CODE,
)

if TYPE_CHECKING:
    from tractor.msg import Aid
    from tractor._addr import (
        UnwrappedAddress,
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
async def test_cancel_remote_arbiter(
    daemon: subprocess.Popen,
    reg_addr: UnwrappedAddress,
):
    assert not current_actor().is_arbiter
    async with tractor.get_registry(reg_addr) as portal:
        await portal.cancel_actor()

    time.sleep(0.1)
    # the arbiter channel server is cancelled but not its main task
    assert daemon.returncode is None

    # no arbiter socket should exist
    with pytest.raises(OSError):
        async with tractor.get_registry(reg_addr) as portal:
            pass


def test_register_duplicate_name(
    daemon: subprocess.Popen,
    reg_addr: UnwrappedAddress,
):
    async def main():
        async with tractor.open_nursery(
            registry_addrs=[reg_addr],
        ) as an:

            assert not current_actor().is_arbiter

            p1 = await an.start_actor('doggy')
            p2 = await an.start_actor('doggy')

            async with tractor.wait_for_actor('doggy') as portal:
                assert portal.channel.uid in (p2.channel.uid, p1.channel.uid)

            await an.cancel()

    # XXX, run manually since we want to start this root **after**
    # the other "daemon" program with it's own root.
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
    from tractor._discovery import get_root
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
):
    '''
    Ensure a non-regristar (serving) root actor can spawn a sub and
    that sub can connect back (manually) to it's rent that is the
    root without issue.

    More or less this audits the global contact info in
    `._state._runtime_vars`.

    '''
    async def main():
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
