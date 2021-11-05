'''
The most hipster way to force SC onto the stdlib's "async".

'''
import asyncio

import pytest
import tractor


async def sleep_and_err():
    await asyncio.sleep(0.1)
    assert 0


async def asyncio_actor():
    assert tractor.current_actor().is_infected_aio()

    await tractor.to_asyncio.run_task(sleep_and_err)


def test_aio_simple_error(arb_addr):

    async def main():
        async with tractor.open_nursery() as n:
            await n.run_in_actor(asyncio_actor, infected_asyncio=True)

    with pytest.raises(tractor.RemoteActorError) as excinfo:
        tractor.run(main, arbiter_addr=arb_addr)


def test_aio_cancel_from_trio(arb_addr):
    ...


def test_aio_cancelled_from_aio_causes_trio_cancelled(arb_addr):
    ...


def test_trio_cancels_aio(arb_addr):
    ...


def test_trio_error_cancels_aio(arb_addr):
    ...


def test_basic_interloop_channel_stream(arb_addr):
    ...


def test_basic_interloop_channel_stream(arb_addr):
    ...


def test_trio_cancels_and_channel_exits(arb_addr):
    ...


def test_aio_errors_and_channel_propagates(arb_addr):
    ...
