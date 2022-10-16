"""
Shared mem primitives and APIs.

"""

# import numpy
import pytest
import trio
import tractor
from tractor._shm import (
    open_shm_list,
    attach_shm_list,
)


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
