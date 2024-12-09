'''
Fast fail test with a `Context`.

Ensure the partially initialized sub-actor process
doesn't cause a hang on error/cancel of the parent
nursery.

'''
import trio
import tractor


@tractor.context
async def sleep(
    ctx: tractor.Context,
):
    await trio.sleep(0.5)
    await ctx.started()
    await trio.sleep_forever()


async def open_ctx(
    n: tractor._supervise.ActorNursery
):

    # spawn both actors
    portal = await n.start_actor(
        name='sleeper',
        enable_modules=[__name__],
    )

    async with portal.open_context(
        sleep,
    ) as (ctx, first):
        assert first is None


async def main():

    async with tractor.open_nursery(
        debug_mode=True,
        loglevel='runtime',
    ) as an:

        async with trio.open_nursery() as n:
            n.start_soon(open_ctx, an)

            await trio.sleep(0.2)
            await trio.sleep(0.1)
            assert 0


if __name__ == '__main__':
    trio.run(main)
