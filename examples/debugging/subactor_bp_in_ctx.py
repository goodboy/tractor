import tractor
import trio


async def gen():
    yield 'yo'
    await tractor.breakpoint()
    yield 'yo'


@tractor.context
async def just_bp(
    ctx: tractor.Context,
) -> None:

    await ctx.started('yo bpin here')
    await tractor.breakpoint()

    # async for val in gen():
    #     print(val)

    await trio.sleep(0.5)

    # THIS CAUSES AN UNRECOVERABLE HANG!?
    assert 0



async def main():
    async with tractor.open_nursery(
        loglevel='transport',
        debug_mode=True,
    ) as n:
        p = await n.start_actor(
            'bp_boi',
            enable_modules=[__name__],
            # debug_mode=True,
        )
        async with p.open_context(
            just_bp,
        ) as (ctx, first):

            # await tractor.breakpoint()
            # breakpoint()
            await trio.sleep_forever()


if __name__ == '__main__':
    trio.run(main)
