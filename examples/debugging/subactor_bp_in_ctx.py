import tractor
import trio


@tractor.context
async def just_bp(
    ctx: tractor.Context,
) -> None:

    await tractor.breakpoint()
    await ctx.started('yo bpin here')


async def main():
    async with tractor.open_nursery(
        debug_mode=True,
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
