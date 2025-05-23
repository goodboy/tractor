'''
Test a service style daemon that maintains a nursery for spawning
"remote async tasks" including both spawning other long living
sub-sub-actor daemons.

'''
from typing import Optional
import asyncio
from contextlib import (
    asynccontextmanager as acm,
    aclosing,
)

import pytest
import trio
import tractor
from tractor import RemoteActorError


async def aio_streamer(
    from_trio: asyncio.Queue,
    to_trio: trio.abc.SendChannel,
) -> trio.abc.ReceiveChannel:

    # required first msg to sync caller
    to_trio.send_nowait(None)

    from itertools import cycle
    for i in cycle(range(10)):
        to_trio.send_nowait(i)
        await asyncio.sleep(0.01)


async def trio_streamer():
    from itertools import cycle
    for i in cycle(range(10)):
        yield i
        await trio.sleep(0.01)


async def trio_sleep_and_err(delay: float = 0.5):
    await trio.sleep(delay)
    # name error
    doggy()  # noqa


_cached_stream: Optional[
    trio.abc.ReceiveChannel
] = None


@acm
async def wrapper_mngr(
):
    from tractor.trionics import broadcast_receiver
    global _cached_stream
    in_aio = tractor.current_actor().is_infected_aio()

    if in_aio:
        if _cached_stream:

            from_aio = _cached_stream

            # if we already have a cached feed deliver a rx side clone
            # to consumer
            async with broadcast_receiver(from_aio, 6) as from_aio:
                yield from_aio
                return
        else:
            async with tractor.to_asyncio.open_channel_from(
                aio_streamer,
            ) as (first, from_aio):
                assert not first

                # cache it so next task uses broadcast receiver
                _cached_stream = from_aio

                yield from_aio
    else:
        async with aclosing(trio_streamer()) as stream:
            # cache it so next task uses broadcast receiver
            _cached_stream = stream
            yield stream


_nursery: trio.Nursery = None


@tractor.context
async def trio_main(
    ctx: tractor.Context,
):
    # sync
    await ctx.started()

    # stash a "service nursery" as "actor local" (aka a Python global)
    global _nursery
    tn = _nursery
    assert tn

    async def consume_stream():
        async with wrapper_mngr() as stream:
            async for msg in stream:
                print(msg)

    # run 2 tasks to ensure broadcaster chan use
    tn.start_soon(consume_stream)
    tn.start_soon(consume_stream)

    tn.start_soon(trio_sleep_and_err)

    await trio.sleep_forever()


@tractor.context
async def open_actor_local_nursery(
    ctx: tractor.Context,
):
    global _nursery
    async with trio.open_nursery(
        strict_exception_groups=False,
    ) as tn:
        _nursery = tn
        await ctx.started()
        await trio.sleep(10)
        # await trio.sleep(1)

        # XXX: this causes the hang since
        # the caller does not unblock from its own
        # ``trio.sleep_forever()``.

        # TODO: we need to test a simple ctx task starting remote tasks
        # that error and then blocking on a ``Nursery.start()`` which
        # never yields back.. aka a scenario where the
        # ``tractor.context`` task IS NOT in the service n's cancel
        # scope.
        tn.cancel_scope.cancel()


@pytest.mark.parametrize(
    'asyncio_mode',
    [True, False],
    ids='asyncio_mode={}'.format,
)
def test_actor_managed_trio_nursery_task_error_cancels_aio(
    asyncio_mode: bool,
    reg_addr: tuple,
):
    '''
    Verify that a ``trio`` nursery created managed in a child actor
    correctly relays errors to the parent actor when one of its spawned
    tasks errors even when running in infected asyncio mode and using
    broadcast receivers for multi-task-per-actor subscription.

    '''
    async def main():

        # cancel the nursery shortly after boot
        async with tractor.open_nursery() as n:
            p = await n.start_actor(
                'nursery_mngr',
                infect_asyncio=asyncio_mode,  # TODO, is this enabling debug mode?
                enable_modules=[__name__],
            )
            async with (
                p.open_context(open_actor_local_nursery) as (ctx, first),
                p.open_context(trio_main) as (ctx, first),
            ):
                await trio.sleep_forever()

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    # verify boxed error
    err = excinfo.value
    assert err.boxed_type is NameError
