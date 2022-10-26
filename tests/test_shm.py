"""
Shared mem primitives and APIs.

"""
import uuid
import platform

# import numpy
import os
import pytest
import trio
import tractor
import _posixshmem
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
        shml = attach_shm_list(
            key=shm_key,
            readonly=False,
        )
        assert shml.shm.name == shm_key
        await trio.sleep(0.001)
        # os.close(shml.shm._fd)



def test_child_attaches_alot():
    async def main():
        async with tractor.open_nursery() as an:

            osx_shm_shared_mem_char_limit = 31

            # allocate writeable list in parent
            key = f'shml_{uuid.uuid4()}'
            if (platform.uname().system == 'Darwin'):
                key = key.replace('-', '')[:osx_shm_shared_mem_char_limit - 1]
        
            shml = open_shm_list(
                key=key,
            )

            portal = await an.start_actor(
                'shm_attacher',
                enable_modules=[__name__],
            )

            async with (
                portal.open_context(
                    child_attach_shml_alot,
                    shm_key=shml.key,
                ) as (ctx, start_val),
            ):
                assert start_val == key
                await ctx.result()

            await portal.cancel_actor()
            # os.close(shml.shm._fd)

    trio.run(main)


@tractor.context
async def child_read_shm_list(
    ctx: tractor.Context,
    shm_key: str,
    use_str: bool,
    frame_size: int,
) -> None:

    # attach in child
    shml = attach_shm_list(key=shm_key)
    await ctx.started(shml.key)

    async with ctx.open_stream() as stream:
        async for i in stream:
            print(f'(child): reading shm list index: {i}')

            if use_str:
                expect = str(float(i))
            else:
                expect = float(i)

            if frame_size == 1:
                val = shml[i]
                assert expect == val
                print(f'(child): reading value: {val}')
            else:
                frame = shml[i - frame_size:i]
                print(f'(child): reading frame: {frame}')

        # os.close(shml.shm._fd)


@pytest.mark.parametrize(
    'use_str', [False, True],
)
@pytest.mark.parametrize(
    'frame_size',
    [1, 2**6, 2**10],
    ids=lambda i: f'frame_size={i}',
)
def test_parent_writer_child_reader(
    use_str: bool,
    frame_size: int,
):

    async def main():
        async with tractor.open_nursery(
            debug_mode=True,
        ) as an:

            portal = await an.start_actor(
                'shm_reader',
                enable_modules=[__name__],
                debug_mode=True,
            )


            # allocate writeable list in parent
            key = 'shm_list'             
            seq_size = int(2 * 2 ** 10)
            shml = open_shm_list(
                key=key,
                size=seq_size,
                readonly=False,
            )

            async with (
                portal.open_context(
                    child_read_shm_list,
                    shm_key=key,
                    use_str=use_str,
                    frame_size=frame_size,
                ) as (ctx, sent),

                ctx.open_stream() as stream,
            ):

                assert sent == key

                for i in range(seq_size):

                    val = float(i)
                    if use_str:
                        val = str(val)

                    print(f'(parent): writing {val}')
                    shml[i] = val

                    # only on frame fills do we
                    # signal to the child that a frame's
                    # worth is ready.
                    if (i % frame_size) == 0:
                        print(f'(parent): signalling frame full on {val}')
                        await stream.send(i)
                else:
                    print(f'(parent): signalling final frame on {val}')
                    await stream.send(i)

            await portal.cancel_actor()
            # os.close(shml.shm._fd)

    trio.run(main)
