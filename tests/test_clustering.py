import itertools

import trio
import tractor
from tractor import open_actor_cluster
from tractor.trionics import async_enter_all

from conftest import tractor_test


MESSAGE = 'tractoring at full speed'


@tractor.context
async def worker(ctx: tractor.Context) -> None:
    await ctx.started()
    async with ctx.open_stream() as stream:
        async for msg in stream:
            # do something with msg
            print(msg)
            assert msg == MESSAGE


@tractor_test
async def test_streaming_to_actor_cluster() -> None:
    teardown_trigger = trio.Event()
    async with (
        open_actor_cluster(modules=[__name__]) as portals,
        async_enter_all(
            mngrs=[p.open_context(worker) for p in portals.values()],
            teardown_trigger=teardown_trigger,
        ) as contexts,
        async_enter_all(
            mngrs=[ctx[0].open_stream() for ctx in contexts],
            teardown_trigger=teardown_trigger,
        ) as streams,
    ):
        with trio.move_on_after(1):
            for stream in itertools.cycle(streams):
                await stream.send(MESSAGE)
        teardown_trigger.set()