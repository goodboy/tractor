"""
Actor "discovery" testing
"""
import os
import signal
import platform
from functools import partial
import itertools

import pytest
import tractor
import trio

from conftest import tractor_test


@tractor_test
async def test_reg_then_unreg(arb_addr):
    actor = tractor.current_actor()
    assert actor.is_arbiter
    assert len(actor._registry) == 1  # only self is registered

    async with tractor.open_nursery() as n:
        portal = await n.start_actor('actor', rpc_module_paths=[__name__])
        uid = portal.channel.uid

        async with tractor.get_arbiter(*arb_addr) as aportal:
            # this local actor should be the arbiter
            assert actor is aportal.actor

            async with tractor.wait_for_actor('actor'):
                # sub-actor uid should be in the registry
                assert uid in aportal.actor._registry
                sockaddrs = actor._registry[uid]
                # XXX: can we figure out what the listen addr will be?
                assert sockaddrs

        await n.cancel()  # tear down nursery

        await trio.sleep(0.1)
        assert uid not in aportal.actor._registry
        sockaddrs = actor._registry[uid]
        assert not sockaddrs


the_line = 'Hi my name is {}'


async def hi():
    return the_line.format(tractor.current_actor().name)


async def say_hello(other_actor):
    await trio.sleep(1)  # wait for other actor to spawn
    async with tractor.find_actor(other_actor) as portal:
        assert portal is not None
        return await portal.run(__name__, 'hi')


async def say_hello_use_wait(other_actor):
    async with tractor.wait_for_actor(other_actor) as portal:
        assert portal is not None
        result = await portal.run(__name__, 'hi')
        return result


@tractor_test
@pytest.mark.parametrize('func', [say_hello, say_hello_use_wait])
async def test_trynamic_trio(func, start_method):
    """Main tractor entry point, the "master" process (for now
    acts as the "director").
    """
    async with tractor.open_nursery() as n:
        print("Alright... Action!")

        donny = await n.run_in_actor(
            'donny',
            func,
            other_actor='gretchen',
        )
        gretchen = await n.run_in_actor(
            'gretchen',
            func,
            other_actor='donny',
        )
        print(await gretchen.result())
        print(await donny.result())
        print("CUTTTT CUUTT CUT!!?! Donny!! You're supposed to say...")


async def stream_forever():
    for i in itertools.count():
        yield i
        await trio.sleep(0.01)


async def cancel(use_signal, delay=0):
    # hold on there sally
    await trio.sleep(delay)

    # trigger cancel
    if use_signal:
        if platform.system() == 'Windows':
            pytest.skip("SIGINT not supported on windows")
        os.kill(os.getpid(), signal.SIGINT)
    else:
        raise KeyboardInterrupt


async def stream_from(portal):
    async for value in await portal.result():
        print(value)


async def spawn_and_check_registry(
    arb_addr: tuple,
    use_signal: bool,
    remote_arbiter: bool = False,
    with_streaming: bool = False,
) -> None:
    actor = tractor.current_actor()

    if remote_arbiter:
        assert not actor.is_arbiter

    async with tractor.get_arbiter(*arb_addr) as portal:
        if actor.is_arbiter:
            async def get_reg():
                return actor._registry
            extra = 1  # arbiter is local root actor
        else:
            get_reg = partial(portal.run, 'self', 'get_registry')
            extra = 2  # local root actor + remote arbiter

        # ensure current actor is registered
        registry = await get_reg()
        assert actor.uid in registry

        if with_streaming:
            to_run = stream_forever
        else:
            to_run = trio.sleep_forever

        async with trio.open_nursery() as trion:
            try:
                async with tractor.open_nursery() as n:
                    portals = {}
                    for i in range(3):
                        name = f'a{i}'
                        portals[name] = await n.run_in_actor(name, to_run)

                    # wait on last actor to come up
                    async with tractor.wait_for_actor(name):
                        registry = await get_reg()
                        for uid in n._children:
                            assert uid in registry

                    assert len(portals) + extra == len(registry)

                    if with_streaming:
                        await trio.sleep(0.1)

                        pts = list(portals.values())
                        for p in pts[:-1]:
                            trion.start_soon(stream_from, p)

                        # stream for 1 sec
                        trion.start_soon(cancel, use_signal, 1)

                        last_p = pts[-1]
                        async for value in await last_p.result():
                            print(value)
                    else:
                        await cancel(use_signal)

            finally:
                with trio.CancelScope(shield=True):
                    await trio.sleep(0.5)

                    # all subactors should have de-registered
                    registry = await get_reg()
                    assert len(registry) == extra
                    assert actor.uid in registry


@pytest.mark.parametrize('use_signal', [False, True])
@pytest.mark.parametrize('with_streaming', [False, True])
def test_subactors_unregister_on_cancel(
    start_method,
    use_signal,
    arb_addr,
    with_streaming,
):
    """Verify that cancelling a nursery results in all subactors
    deregistering themselves with the arbiter.
    """
    with pytest.raises(KeyboardInterrupt):
        tractor.run(
            spawn_and_check_registry,
            arb_addr,
            use_signal,
            False,
            with_streaming,
            arbiter_addr=arb_addr
        )


@pytest.mark.parametrize('use_signal', [False, True])
@pytest.mark.parametrize('with_streaming', [False, True])
def test_subactors_unregister_on_cancel_remote_daemon(
    daemon,
    start_method,
    use_signal,
    arb_addr,
    with_streaming,
):
    """Verify that cancelling a nursery results in all subactors
    deregistering themselves with a **remote** (not in the local process
    tree) arbiter.
    """
    with pytest.raises(KeyboardInterrupt):
        tractor.run(
            spawn_and_check_registry,
            arb_addr,
            use_signal,
            True,
            with_streaming,
            # XXX: required to use remote daemon!
            arbiter_addr=arb_addr
        )
