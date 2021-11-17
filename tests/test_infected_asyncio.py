'''
The most hipster way to force SC onto the stdlib's "async".

'''
from typing import Optional
import asyncio
import builtins
import importlib

import pytest
import trio
import tractor
from tractor import RemoteActorError


async def sleep_and_err():
    await asyncio.sleep(0.1)
    assert 0


async def sleep_forever():
    await asyncio.sleep(float('inf'))


async def asyncio_actor(

    target: str,
    expect_err: Optional[Exception] = None

) -> None:

    assert tractor.current_actor().is_infected_aio()
    target = globals()[target]

    if '.' in expect_err:
        modpath, _, name = expect_err.rpartition('.')
        mod = importlib.import_module(modpath)
        error_type = getattr(mod, name)

    else:  # toplevel builtin error type
        error_type = builtins.__dict__.get(expect_err)

    try:
        # spawn an ``asyncio`` task to run a func and return result
        await tractor.to_asyncio.run_task(target)

    except BaseException as err:
        if expect_err:
            assert isinstance(err, error_type)

        raise err


def test_aio_simple_error(arb_addr):
    '''
    Verify a simple remote asyncio error propagates back through trio
    to the parent actor.


    '''
    async def main():
        async with tractor.open_nursery(
            arbiter_addr=arb_addr
        ) as n:
            await n.run_in_actor(
                asyncio_actor,
                target='sleep_and_err',
                expect_err='AssertionError',
                infect_asyncio=True,
            )

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    err = excinfo.value
    assert isinstance(err, RemoteActorError)
    assert err.type == AssertionError


def test_tractor_cancels_aio(arb_addr):
    '''
    Verify we can cancel a spawned asyncio task gracefully.

    '''
    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.run_in_actor(
                asyncio_actor,
                target='sleep_forever',
                expect_err='trio.Cancelled',
                infect_asyncio=True,
            )
            # cancel the entire remote runtime
            await portal.cancel_actor()

    trio.run(main)


async def aio_cancel():
    ''''Cancel urself boi.

    '''
    await asyncio.sleep(0.5)
    task = asyncio.current_task()

    # cancel and enter sleep
    task.cancel()
    await sleep_forever()


def test_aio_cancelled_from_aio_causes_trio_cancelled(arb_addr):

    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.run_in_actor(
                asyncio_actor,
                target='aio_cancel',
                expect_err='asyncio.CancelledError',
                infect_asyncio=True,
            )

            # with trio.CancelScope(shield=True):
            await portal.result()

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)


def test_trio_cancels_aio(arb_addr):
    ...


def test_trio_error_cancels_aio(arb_addr):
    ...


def test_basic_interloop_channel_stream(arb_addr):
    ...


def test_trio_cancels_and_channel_exits(arb_addr):
    ...


def test_aio_errors_and_channel_propagates(arb_addr):
    ...
