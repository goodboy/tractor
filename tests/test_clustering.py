import itertools

import trio
import tractor
from tractor import open_actor_cluster
from tractor.trionics import gather_contexts

from conftest import tractor_test


MESSAGE = 'tractoring at full speed'


@tractor.context
async def worker(ctx: tractor.Context) -> None:
    await ctx.started()
    async with ctx.open_stream(backpressure=True) as stream:
        async for msg in stream:
            # do something with msg
            print(msg)
            assert msg == MESSAGE


@tractor_test
async def test_streaming_to_actor_cluster() -> None:
    async with (
        open_actor_cluster(modules=[__name__]) as portals,
        gather_contexts(
            mngrs=[p.open_context(worker) for p in portals.values()],
        ) as contexts,
        gather_contexts(
            mngrs=[ctx[0].open_stream() for ctx in contexts],
        ) as streams,
    ):
        with trio.move_on_after(1):
            for stream in itertools.cycle(streams):
                await stream.send(MESSAGE)
