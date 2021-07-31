import trio
import tractor


@tractor.context
async def simple_rpc(

    ctx: tractor.Context,
    data: int,

) -> None:
    '''Test a small ping-pong 2-way streaming server.

    '''
    # signal to parent that we're up much like
    # ``trio_typing.TaskStatus.started()``
    await ctx.started(data + 1)

    async with ctx.open_stream() as stream:

        count = 0
        async for msg in stream:

            assert msg == 'ping'
            await stream.send('pong')
            count += 1

        else:
            assert count == 10


async def main() -> None:

    async with tractor.open_nursery() as n:

        portal = await n.start_actor(
            'rpc_server',
            enable_modules=[__name__],
        )

        # XXX: syntax requires py3.9
        async with (

            portal.open_context(
                simple_rpc,  # taken from pytest parameterization
                data=10,

            ) as (ctx, sent),

            ctx.open_stream() as stream,
        ):

            assert sent == 11

            count = 0
            # receive msgs using async for style
            await stream.send('ping')

            async for msg in stream:
                assert msg == 'pong'
                await stream.send('ping')
                count += 1

                if count >= 9:
                    break

        # explicitly teardown the daemon-actor
        await portal.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
