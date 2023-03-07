import asyncio

import trio
import tractor


async def bp_then_error(
    to_trio: trio.MemorySendChannel,
    from_trio: asyncio.Queue,

) -> None:

    # sync with ``trio``-side (caller) task
    to_trio.send_nowait('start')

    # NOTE: what happens here inside the hook needs some refinement..
    # => seems like it's still `._debug._set_trace()` but
    #    we set `Lock.local_task_in_debug = 'sync'`, we probably want
    #    some further, at least, meta-data about the task/actoq in debug
    #    in terms of making it clear it's asyncio mucking about.

    breakpoint()

    await asyncio.sleep(0.5)
    raise ValueError('blah')


async def aio_sleep_forever():
    await asyncio.sleep(float('inf'))


@tractor.context
async def trio_ctx(
    ctx: tractor.Context,
):

    # this will block until the ``asyncio`` task sends a "first"
    # message, see first line in above func.
    async with (
        tractor.to_asyncio.open_channel_from(bp_then_error) as (first, chan),
        trio.open_nursery() as n,
    ):

        assert first == 'start'
        await ctx.started(first)

        n.start_soon(
            tractor.to_asyncio.run_task,
            aio_sleep_forever,
        )
        await trio.sleep_forever()


async def main():

    async with tractor.open_nursery() as n:

        p = await n.start_actor(
            'aio_daemon',
            enable_modules=[__name__],
            infect_asyncio=True,
            debug_mode=True,
            loglevel='cancel',
        )

        async with p.open_context(trio_ctx) as (ctx, first):

            assert first == 'start'
            await trio.sleep_forever()

            assert 0

        # TODO: case where we cancel from trio-side while asyncio task
        # has debugger lock?
        # await p.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
