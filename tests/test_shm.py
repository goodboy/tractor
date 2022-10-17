"""
Shared mem primitives and APIs.

"""
import uuid

# import numpy
import pytest
import trio
import tractor
from tractor._shm import (
    open_shm_list,
    attach_shm_list,
)


@tractor.context
async def child_attach_shml_alot(
    ctx: tractor.Context,
    shm_key: str,
) -> None:

    await ctx.started(shm_key)

    # now try to attach a boatload of times in a loop..
    for _ in range(1000):
        shml = attach_shm_list(key=shm_key)
        assert shml.shm.name == shm_key
        await trio.sleep(0.001)


def test_child_attaches_alot():
    async def main():
        async with tractor.open_nursery() as an:

            # allocate writeable list in parent
            key = f'shml_{uuid.uuid4()}'
            shml = open_shm_list(
                key=key,
            )

            portal = await an.start_actor(
                'shm_attacher',
                enable_modules=[__name__],
            )

            async with (
                portal.open_context(
                    child_attach_shml_alot,  # taken from pytest parameterization
                    shm_key=key,
                ) as (ctx, start_val),
            ):
                assert start_val == key
                await ctx.result()

            await portal.cancel_actor()

    trio.run(main)


@tractor.context
async def child_read_shm_list(
    ctx: tractor.Context,
    shm_key: str,
    use_str: bool,
) -> None:

    shml = attach_shm_list(key=shm_key)
    await ctx.started(shml.key)

    async with ctx.open_stream() as stream:
        async for i in stream:
            print(f'reading shm list index: {i}')

            if use_str:
                expect = str(float(i))
            else:
                expect = float(i)

            assert expect == shml[i]


@pytest.mark.parametrize(
    'use_str', [False, True],
)
def test_parent_writer_child_reader(
    use_str: bool,
):

    async def main():
        async with tractor.open_nursery() as an:

            # allocate writeable list in parent
            key = 'shm_list'
            shml = open_shm_list(
                key=key,
                readonly=False,
            )

            portal = await an.start_actor(
                'shm_reader',
                enable_modules=[__name__],
            )

            async with (
                portal.open_context(
                    child_read_shm_list,  # taken from pytest parameterization
                    shm_key=key,
                    use_str=use_str,
                ) as (ctx, sent),

                ctx.open_stream() as stream,
            ):

                assert sent == key

                for i in range(2 ** 10):

                    val = float(i)
                    if use_str:
                        val = str(val)

                    print(f'writing {val}')
                    shml[i] = val
                    await stream.send(i)

            await portal.cancel_actor()

    trio.run(main)
