import trio
import tractor


@tractor.context
async def just_sleep(

    ctx: tractor.Context,
    **kwargs,

) -> None:
    '''
    Start and sleep.

    '''
    await ctx.started()
    await trio.sleep_forever()


async def main() -> None:

    async with tractor.open_nursery(
        debug_mode=True,
    ) as n:
        portal = await n.start_actor(
            'ctx_child',

            # XXX: we don't enable the current module in order
            # to trigger `ModuleNotFound`.
            enable_modules=[],
        )

        async with portal.open_context(
            just_sleep,  # taken from pytest parameterization
        ) as (ctx, sent):
            raise KeyboardInterrupt


if __name__ == '__main__':
    trio.run(main)
