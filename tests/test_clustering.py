import itertools

import pytest
import trio
import tractor
from tractor import open_actor_cluster
from tractor.trionics import gather_contexts
from tractor._testing import tractor_test

MESSAGE = 'tractoring at full speed'


def test_empty_mngrs_input_raises() -> None:

    async def main():
        with trio.fail_after(1):
            async with (
                open_actor_cluster(
                    modules=[__name__],

                    # NOTE: ensure we can passthrough runtime opts
                    loglevel='info',
                    # debug_mode=True,

                ) as portals,

                gather_contexts(
                    # NOTE: it's the use of inline-generator syntax
                    # here that causes the empty input.
                    mngrs=(
                        p.open_context(worker) for p in portals.values()
                    ),
                ),
            ):
                assert 0

    with pytest.raises(ValueError):
        trio.run(main)


@tractor.context
async def worker(
    ctx: tractor.Context,

) -> None:

    await ctx.started()

    async with ctx.open_stream(
        allow_overruns=True,
    ) as stream:

        # TODO: this with the below assert causes a hang bug?
        # with trio.move_on_after(1):

        async for msg in stream:
            # do something with msg
            print(msg)
            assert msg == MESSAGE

        # TODO: does this ever cause a hang
        # assert 0


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
