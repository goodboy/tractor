import tractor
import trio


async def gen():
    yield 'yo'
    await tractor.pause()
    yield 'yo'
    await tractor.pause()


@tractor.context
async def just_bp(
    ctx: tractor.Context,
) -> None:

    await ctx.started()
    await tractor.pause()

    # TODO: bps and errors in this call..
    async for val in gen():
        print(val)

    # await trio.sleep(0.5)

    # prematurely destroy the connection
    await ctx.chan.aclose()

    # THIS CAUSES AN UNRECOVERABLE HANG
    # without latest ``pdbpp``:
    assert 0



async def main():

    async with tractor.open_nursery(
        debug_mode=True,
        enable_transports=['uds'],
        loglevel='devx',
    ) as n:
        p = await n.start_actor(
            'bp_boi',
            enable_modules=[__name__],
        )
        async with p.open_context(
            just_bp,
        ) as (ctx, first):
            await trio.sleep_forever()


if __name__ == '__main__':
    trio.run(main)
