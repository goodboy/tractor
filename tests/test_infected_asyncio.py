'''
The most hipster way to force SC onto the stdlib's "async".

'''
from typing import Optional, Iterable
import asyncio
import builtins
import importlib

import pytest
import trio
import tractor
from tractor import to_asyncio
from tractor import RemoteActorError


async def sleep_and_err():
    await asyncio.sleep(0.1)
    assert 0


async def sleep_forever():
    await asyncio.sleep(float('inf'))


async def trio_cancels_single_aio_task():

    # spawn an ``asyncio`` task to run a func and return result
    with trio.move_on_after(.2):
        await tractor.to_asyncio.run_task(sleep_forever)


def test_trio_cancels_aio_on_actor_side(arb_addr):
    '''
    Spawn an infected actor that is cancelled by the ``trio`` side
    task using std cancel scope apis.

    '''
    async def main():
        async with tractor.open_nursery(
            arbiter_addr=arb_addr
        ) as n:
            await n.run_in_actor(
                trio_cancels_single_aio_task,
                infect_asyncio=True,
            )

    trio.run(main)


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


def test_trio_cancels_aio(arb_addr):
    '''
    Much like the above test with ``tractor.Portal.cancel_actor()``
    except we just use a standard ``trio`` cancellation api.

    '''
    async def main():

        with trio.move_on_after(1):
            # cancel the nursery shortly after boot

            async with tractor.open_nursery() as n:
                # debug_mode=True
            # ) as n:
                portal = await n.run_in_actor(
                    asyncio_actor,
                    target='sleep_forever',
                    expect_err='trio.Cancelled',
                    infect_asyncio=True,
                )

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


# TODO:
async def no_to_trio_in_args():
    pass


async def push_from_aio_task(

    sequence: Iterable,
    to_trio: trio.abc.SendChannel,

) -> None:
    for i in range(100):
        print(f'asyncio sending {i}')
        to_trio.send_nowait(i)
        await asyncio.sleep(0.001)

    print(f'asyncio streamer complete!')


async def stream_from_aio():
    seq = range(100)
    expect = list(seq)

    async with to_asyncio.open_channel_from(
        push_from_aio_task,
        sequence=seq,
    ) as (first, chan):

        pulled = [first]
        async for value in chan:
            print(f'trio received {value}')
            pulled.append(value)

        assert pulled == expect

    print('trio guest mode task completed!')


def test_basic_interloop_channel_stream(arb_addr):
    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.run_in_actor(
                stream_from_aio,
                infect_asyncio=True,
            )
            await portal.result()

    trio.run(main)



# def test_trio_error_cancels_intertask_chan(arb_addr):
#     ...


# def test_trio_cancels_and_channel_exits(arb_addr):
#     ...


# def test_aio_errors_and_channel_propagates(arb_addr):
#     ...
