'''
Examples of using the builtin `breakpoint()` from an `asyncio.Task`
running in a subactor spawned with `infect_asyncio=True`.

'''
import asyncio

import trio
import tractor
from tractor import (
    to_asyncio,
    Portal,
)


async def aio_sleep_forever():
    await asyncio.sleep(float('inf'))


async def bp_then_error(
    to_trio: trio.MemorySendChannel,
    from_trio: asyncio.Queue,

    raise_after_bp: bool = True,

) -> None:

    # sync with `trio`-side (caller) task
    to_trio.send_nowait('start')

    # NOTE: what happens here inside the hook needs some refinement..
    # => seems like it's still `._debug._set_trace()` but
    #    we set `Lock.local_task_in_debug = 'sync'`, we probably want
    #    some further, at least, meta-data about the task/actor in debug
    #    in terms of making it clear it's `asyncio` mucking about.
    breakpoint()  # asyncio-side

    # short checkpoint / delay
    await asyncio.sleep(0.5)  # asyncio-side

    if raise_after_bp:
        raise ValueError('asyncio side error!')

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
            # raise_after_bp=not bp_before_started,
        ) as (first, chan),

        trio.open_nursery() as tn,
    ):
        assert first == 'start'

        if bp_before_started:
            await tractor.pause()  # trio-side

        await ctx.started(first)  # trio-side

        tn.start_soon(
            to_asyncio.run_task,
            aio_sleep_forever,
        )
        await trio.sleep_forever()


async def main(
    bps_all_over: bool = True,

    # TODO, WHICH OF THESE HAZ BUGZ?
    cancel_from_root: bool = False,
    err_from_root: bool = False,

) -> None:

    async with tractor.open_nursery(
        debug_mode=True,
        maybe_enable_greenback=True,
        # loglevel='devx',
    ) as an:
        ptl: Portal = await an.start_actor(
            'aio_daemon',
            enable_modules=[__name__],
            infect_asyncio=True,
            debug_mode=True,
            # loglevel='cancel',
        )

        async with ptl.open_context(
            trio_ctx,
            bp_before_started=bps_all_over,
        ) as (ctx, first):

            assert first == 'start'

            # pause in parent to ensure no cross-actor
            # locking problems exist!
            await tractor.pause()  # trio-root

            if cancel_from_root:
                await ctx.cancel()

            if err_from_root:
                assert 0
            else:
                await trio.sleep_forever()


        # TODO: case where we cancel from trio-side while asyncio task
        # has debugger lock?
        # await ptl.cancel_actor()


if __name__ == '__main__':

    # works fine B)
    trio.run(main)

    # will hang and ignores SIGINT !!
    # NOTE: you'll need to send a SIGQUIT (via ctl-\) to kill it
    # manually..
    # trio.run(main, True)
