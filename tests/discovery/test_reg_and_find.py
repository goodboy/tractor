'''
Basic registration + actor discovery via the registrar.

'''
from typing import Callable

import pytest
import tractor
from tractor._testing import tractor_test
import trio


@tractor_test
async def test_reg_then_unreg(
    reg_addr: tuple,
):
    actor = tractor.current_actor()
    assert actor.is_registrar
    assert len(actor._registry) == 1  # only self is registered

    async with tractor.open_nursery(
        registry_addrs=[reg_addr],
    ) as n:

        portal = await n.start_actor('actor', enable_modules=[__name__])
        uid = portal.channel.aid.uid

        async with tractor.get_registry(reg_addr) as aportal:
            # this local actor should be the registrar
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
        sockaddrs = actor._registry.get(uid)
        assert not sockaddrs


the_line = 'Hi my name is {}'


async def hi():
    return the_line.format(tractor.current_actor().name)


async def say_hello(
    other_actor: str,
    reg_addr: tuple[str, int],
):
    await trio.sleep(1)  # wait for other actor to spawn
    async with tractor.find_actor(
        other_actor,
        registry_addrs=[reg_addr],
    ) as portal:
        assert portal is not None
        return await portal.run(__name__, 'hi')


async def say_hello_use_wait(
    other_actor: str,
    reg_addr: tuple[str, int],
):
    async with tractor.wait_for_actor(
        other_actor,
        registry_addr=reg_addr,
    ) as portal:
        assert portal is not None
        result = await portal.run(__name__, 'hi')
        return result


@tractor_test
@pytest.mark.parametrize(
    'func',
    [say_hello,
     say_hello_use_wait]
)
async def test_trynamic_trio(
    func: Callable,
    start_method: str,
    reg_addr: tuple,
):
    '''
    Root actor acting as the "director" and running one-shot-task-actors
    for the directed subs.

    '''
    async with tractor.open_nursery() as n:
        print("Alright... Action!")

        donny = await n.run_in_actor(
            func,
            other_actor='gretchen',
            reg_addr=reg_addr,
            name='donny',
        )
        gretchen = await n.run_in_actor(
            func,
            other_actor='donny',
            reg_addr=reg_addr,
            name='gretchen',
        )
        print(await gretchen.result())
        print(await donny.result())
        print("CUTTTT CUUTT CUT!!?! Donny!! You're supposed to say...")
