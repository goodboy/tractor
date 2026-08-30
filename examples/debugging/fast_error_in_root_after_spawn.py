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
) -> None:
    '''
    Start a context after a brief initialization delay.

    '''
    await trio.sleep(0.5)
    await ctx.started()
    await trio.sleep_forever()


async def open_ctx(
    n: tractor.runtime._supervise.ActorNursery
) -> None:
    '''
    Spawn a sleeper and open a context with it.

    '''
    # spawn both actors
    portal: tractor.Portal = await n.start_actor(
        name='sleeper',
        enable_modules=[__name__],
    )

    async with portal.open_context(
        sleep,
    ) as (ctx, first):
        assert first is None


async def main() -> None:
    '''
    Fail the root while a subactor context is still starting.

    '''
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
