import trio
from tractor import (
    open_nursery,
    context,
    Context,
    MsgStream,
)


async def break_channel_silently(
    stream: MsgStream,
):
    async for msg in stream:
        await stream.send(msg)
        # XXX: close the channel right after an error is raised
        # purposely breaking the IPC transport to make sure the parent
        # doesn't get stuck in debug. this more or less simulates an
        # infinite msg-receive hang on the other end. await
        await stream._ctx.chan.send(None)
        assert 0


async def error_and_break_stream(
    stream: MsgStream,
):
    async for msg in stream:
        # echo back msg
        await stream.send(msg)

        try:
            assert 0
        except Exception:
            await stream._ctx.chan.send(None)

            # NOTE: doing this instead causes the error to propagate
            # correctly.
            # await stream.aclose()
            raise


@context
async def just_sleep(

    ctx: Context,
    **kwargs,

) -> None:
    '''
    Start and sleep.

    '''
    d = {}
    await ctx.started()
    try:
        async with (
            ctx.open_stream() as stream,
            trio.open_nursery() as n,
        ):
            for i in range(100):
                await stream.send(i)
                if i > 50:
                    n.start_soon(break_channel_silently, stream)
                    n.start_soon(error_and_break_stream, stream)

    finally:
        d['10'] = 10


async def main() -> None:

    async with open_nursery(
        # loglevel='info',
        debug_mode=True,
    ) as n:
        portal = await n.start_actor(
            'ctx_child',

            # XXX: we don't enable the current module in order
            # to trigger `ModuleNotFound`.
            enable_modules=[__name__],

            # add a debugger test to verify this works B)
            # debug_mode=True,
        )

        async with portal.open_context(
            just_sleep,  # taken from pytest parameterization
        ) as (ctx, sent):
            async with ctx.open_stream() as stream:
                await stream.send(10)
                async for msg in stream:
                    print(msg)


if __name__ == '__main__':
    trio.run(main)
