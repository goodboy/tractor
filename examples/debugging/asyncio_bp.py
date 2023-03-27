import asyncio

import trio
import tractor
from tractor import to_asyncio


async def aio_sleep_forever():
    await asyncio.sleep(float('inf'))


async def bp_then_error(
    to_trio: trio.MemorySendChannel,
    from_trio: asyncio.Queue,

    raise_after_bp: bool = True,

) -> None:

    # sync with ``trio``-side (caller) task
    to_trio.send_nowait('start')

    # NOTE: what happens here inside the hook needs some refinement..
    # => seems like it's still `._debug._set_trace()` but
    #    we set `Lock.local_task_in_debug = 'sync'`, we probably want
    #    some further, at least, meta-data about the task/actoq in debug
    #    in terms of making it clear it's asyncio mucking about.
    breakpoint()

    # short checkpoint / delay
    await asyncio.sleep(0.5)

    if raise_after_bp:
        raise ValueError('blah')

    # TODO: test case with this so that it gets cancelled?
    else:
        # XXX NOTE: this is required in order to get the SIGINT-ignored
        # hang case documented in the module script section!
        await aio_sleep_forever()


@tractor.context
async def trio_ctx(
    ctx: tractor.Context,
    bp_before_started: bool = False,
):

    # this will block until the ``asyncio`` task sends a "first"
    # message, see first line in above func.
    async with (

        to_asyncio.open_channel_from(
            bp_then_error,
            raise_after_bp=not bp_before_started,
        ) as (first, chan),

        trio.open_nursery() as n,
    ):

        assert first == 'start'

        if bp_before_started:
            await tractor.breakpoint()

        await ctx.started(first)

        n.start_soon(
            to_asyncio.run_task,
            aio_sleep_forever,
        )
        await trio.sleep_forever()


async def main(
    bps_all_over: bool = False,

) -> None:

    async with tractor.open_nursery() as n:

        p = await n.start_actor(
            'aio_daemon',
            enable_modules=[__name__],
            infect_asyncio=True,
            debug_mode=True,
            loglevel='cancel',
        )

        async with p.open_context(
            trio_ctx,
            bp_before_started=bps_all_over,
        ) as (ctx, first):

            assert first == 'start'

            if bps_all_over:
                await tractor.breakpoint()

            # await trio.sleep_forever()
            await ctx.cancel()
            assert 0

        # TODO: case where we cancel from trio-side while asyncio task
        # has debugger lock?
        # await p.cancel_actor()


if __name__ == '__main__':

    # works fine B)
    trio.run(main)

    # will hang and ignores SIGINT !!
    # NOTE: you'll need to send a SIGQUIT (via ctl-\) to kill it
    # manually..
    # trio.run(main, True)
