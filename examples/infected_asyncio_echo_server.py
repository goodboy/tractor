'''
An SC compliant infected ``asyncio`` echo server.

'''
import asyncio
from statistics import mean
import time

import trio
import tractor


async def aio_echo_server(
    chan: tractor.to_asyncio.LinkedTaskChannel,
) -> None:
    '''
    Echo messages received through an asyncio task channel.

    '''
    # a first message must be sent **from** this ``asyncio``
    # task or the ``trio`` side will never unblock from
    # ``tractor.to_asyncio.open_channel_from():``
    chan.started_nowait('start')

    while True:
        # echo the msg back
        chan.send_nowait(await chan.get())
        await asyncio.sleep(0)


@tractor.context
async def trio_to_aio_echo_server(
    ctx: tractor.Context,
) -> None:
    '''
    Bridge an actor stream to the asyncio echo server.

    '''
    # this will block until the ``asyncio`` task sends a "first"
    # message.
    chan: tractor.to_asyncio.LinkedTaskChannel
    first: str
    async with tractor.to_asyncio.open_channel_from(
        aio_echo_server,
    ) as (chan, first):

        assert first == 'start'
        await ctx.started(first)

        stream: tractor.MsgStream
        async with ctx.open_stream() as stream:

            msg: int
            async for msg in stream:
                await chan.send(msg)

                out: int = await chan.receive()
                # echo back to parent actor-task
                await stream.send(out)


async def main() -> None:
    '''
    Run the infected asyncio echo-server example.

    '''
    an: tractor.ActorNursery
    async with tractor.open_nursery() as an:
        portal: tractor.Portal = await an.start_actor(
            'aio_server',
            enable_modules=[__name__],
            infect_asyncio=True,
        )
        ctx: tractor.Context
        first: str
        async with portal.open_context(
            trio_to_aio_echo_server,
        ) as (ctx, first):

            assert first == 'start'

            count: int = 0
            stream: tractor.MsgStream
            async with ctx.open_stream() as stream:

                delays: list[float] = []
                send: float = time.time()

                await stream.send(count)
                msg: int
                async for msg in stream:
                    recv: float = time.time()
                    delays.append(recv - send)
                    assert msg == count
                    count += 1
                    send = time.time()
                    await stream.send(count)

                    if count >= 1e3:
                        break

        print(f'mean round trip rate (Hz): {1/mean(delays)}')
        await portal.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
