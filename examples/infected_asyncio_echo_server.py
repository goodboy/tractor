'''
An SC compliant infected ``asyncio`` echo server.

'''
import asyncio
from statistics import mean
import time

import trio
import tractor


async def aio_echo_server(
    to_trio: trio.MemorySendChannel,
    from_trio: asyncio.Queue,
) -> None:

    # a first message must be sent **from** this ``asyncio``
    # task or the ``trio`` side will never unblock from
    # ``tractor.to_asyncio.open_channel_from():``
    to_trio.send_nowait('start')

    # XXX: this uses an ``from_trio: asyncio.Queue`` currently but we
    # should probably offer something better.
    while True:
        # echo the msg back
        to_trio.send_nowait(await from_trio.get())
        await asyncio.sleep(0)


@tractor.context
async def trio_to_aio_echo_server(
    ctx: tractor.Context,
):
    # this will block until the ``asyncio`` task sends a "first"
    # message.
    async with tractor.to_asyncio.open_channel_from(
        aio_echo_server,
    ) as (first, chan):

        assert first == 'start'
        await ctx.started(first)

        async with ctx.open_stream() as stream:

            async for msg in stream:
                await chan.send(msg)

                out = await chan.receive()
                # echo back to parent actor-task
                await stream.send(out)


async def main():

    async with tractor.open_nursery() as n:
        p = await n.start_actor(
            'aio_server',
            enable_modules=[__name__],
            infect_asyncio=True,
        )
        async with p.open_context(
            trio_to_aio_echo_server,
        ) as (ctx, first):

            assert first == 'start'

            count = 0
            async with ctx.open_stream() as stream:

                delays = []
                send = time.time()

                await stream.send(count)
                async for msg in stream:
                    recv = time.time()
                    delays.append(recv - send)
                    assert msg == count
                    count += 1
                    send = time.time()
                    await stream.send(count)

                    if count >= 1e3:
                        break

        print(f'mean round trip rate (Hz): {1/mean(delays)}')
        await p.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
