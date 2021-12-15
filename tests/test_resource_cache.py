'''
Async context manager cache api testing: ``trionics.maybe_open_context():``

'''
from contextlib import asynccontextmanager as acm
import platform
from typing import Awaitable

import trio
import tractor


@tractor.context
async def streamer(
    ctx: tractor.Context,
    seq: list[int] = list(range(1000)),
) -> None:

    await ctx.started()
    async with ctx.open_stream() as stream:
        for val in seq:
            await stream.send(val)
            await trio.sleep(0.001)

    print('producer finished')


@acm
async def open_stream() -> Awaitable[tractor.MsgStream]:

    async with tractor.open_nursery() as tn:
        portal = await tn.start_actor('streamer', enable_modules=[__name__])
        async with (
            portal.open_context(streamer) as (ctx, first),
            ctx.open_stream() as stream,
        ):
            yield stream

        await portal.cancel_actor()
    print('CANCELLED STREAMER')


@acm
async def maybe_open_stream(taskname: str):
    async with tractor.trionics.maybe_open_context(
        # NOTE: all secondary tasks should cache hit on the same key
        acm_func=open_stream,
    ) as (cache_hit, stream):

        if cache_hit:
            print(f'{taskname} loaded from cache')

            # add a new broadcast subscription for the quote stream
            # if this feed is already allocated by the first
            # task that entereed
            async with stream.subscribe() as bstream:
                yield bstream
        else:
            # yield the actual stream
            yield stream


def test_open_local_sub_to_stream():
    '''
    Verify a single inter-actor stream can can be fanned-out shared to
    N local tasks using ``trionics.maybe_open_context():``.

    '''
    timeout = 3 if platform.system() != "Windows" else 10

    async def main():

        full = list(range(1000))

        async def get_sub_and_pull(taskname: str):
            async with (
                maybe_open_stream(taskname) as stream,
            ):
                if '0' in taskname:
                    assert isinstance(stream, tractor.MsgStream)
                else:
                    assert isinstance(
                        stream,
                        tractor.trionics.BroadcastReceiver
                    )

                first = await stream.receive()
                print(f'{taskname} started with value {first}')
                seq = []
                async for msg in stream:
                    seq.append(msg)

                assert set(seq).issubset(set(full))
            print(f'{taskname} finished')

        # TODO: turns out this isn't multi-task entrant XD
        # We probably need an indepotent entry semantic?
        with trio.fail_after(timeout):
            async with tractor.open_root_actor():
                async with (
                    trio.open_nursery() as nurse,
                ):
                    for i in range(10):
                        nurse.start_soon(get_sub_and_pull, f'task_{i}')
                        await trio.sleep(0.001)

                print('all consumer tasks finished')

    trio.run(main)
