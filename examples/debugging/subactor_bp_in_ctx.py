import platform

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

    if platform.system() != 'Darwin':
        tpt = 'uds'
    else:
        # XXX, precisely we can't use pytest's tmp-path generation
        # for tests.. apparently because:
        #
        # > The OSError: AF_UNIX path too long in macOS Python occurs
        # > because the path to the Unix domain socket exceeds the
        # > operating system's maximum path length limit (around 104
        #
        # WHICH IS just, wtf hillarious XD
        tpt = 'tcp'

    async with tractor.open_nursery(
        debug_mode=True,
        enable_transports=[tpt],
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
