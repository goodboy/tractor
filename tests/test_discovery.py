"""
Actor "discovery" testing
"""
import os
import signal
import platform

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


@pytest.mark.parametrize('use_signal', [False, True])
def test_subactors_unregister_on_cancel(start_method, use_signal):
    """Verify that cancelling a nursery results in all subactors
    deregistering themselves with the arbiter.
    """
    async def main():
        actor = tractor.current_actor()
        assert actor.is_arbiter
        registry = actor._registry

        # arbiter is registered
        assert actor.uid in registry

        try:
            async with tractor.open_nursery() as n:
                portals = {}
                for i in range(3):
                    name = f'a{i}'
                    portals[name] = await n.run_in_actor(
                        name, trio.sleep_forever)

                # wait on last actor to come up
                async with tractor.wait_for_actor(name):
                    for uid in n._children:
                        assert uid in registry

                assert len(portals) + 1 == len(registry)

                # trigger cancel
                if use_signal:
                    if platform.system() == 'Windows':
                        pytest.skip("SIGINT not supported on windows")
                    os.kill(os.getpid(), signal.SIGINT)
                else:
                    raise KeyboardInterrupt
        finally:
            # all subactors should have de-registered
            await trio.sleep(0.5)
            assert len(registry) == 1
            assert actor.uid in registry

    with pytest.raises(KeyboardInterrupt):
        tractor.run(main)
