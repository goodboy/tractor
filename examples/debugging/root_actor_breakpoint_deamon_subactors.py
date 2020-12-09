from functools import partial
import asyncio

import trio
import tractor


async def asyncio_loop_forever():
    while True:
        await asyncio.sleep(0.1)
        # print('yo')


async def sleep_forever(asyncio=False):

    if asyncio:
        await tractor.to_asyncio.run_task(asyncio_loop_forever)
    else:
        await trio.sleep_forever()


async def stream_forever():
    while True:
        yield 'doggy'


async def main():
    """Test breakpoint in a streaming actor.
    """
    await tractor.breakpoint()
    async with tractor.open_nursery() as n:

        p0 = await n.start_actor('streamer',
            rpc_module_paths=[__name__])


        p1 = await n.start_actor(
            'sleep_forever',
            rpc_module_paths=[__name__], infect_asyncio=True
        )
        # await n.run_in_actor('sleeper', sleep_forever)
        # await n.run_in_actor('sleeper', sleep_forever)

        async with trio.open_nursery() as ln:
            ln.start_soon(
                partial(
                    p1.run,
                    __name__,
                    'sleep_forever',
                    asyncio=True,
                )
            )
            # ln.start_soon(p0.run, __name__, 'sleep_forever')
            async for val in await p0.run(__name__, 'stream_forever'):
                print(val)

            await trio.sleep(1)


if __name__ == '__main__':
    tractor.run(main, debug_mode=True, loglevel='trace')
