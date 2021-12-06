"""
Bidirectional streaming.

"""
import pytest
import trio
import tractor


@tractor.context
async def simple_rpc(

    ctx: tractor.Context,
    data: int,

) -> None:
    '''
    Test a small ping-pong server.

    '''
    # signal to parent that we're up
    await ctx.started(data + 1)

    print('opening stream in callee')
    async with ctx.open_stream() as stream:

        count = 0
        while True:
            try:
                await stream.receive() == 'ping'
            except trio.EndOfChannel:
                assert count == 10
                break
            else:
                print('pong')
                await stream.send('pong')
                count += 1


@tractor.context
async def simple_rpc_with_forloop(

    ctx: tractor.Context,
    data: int,

) -> None:
    """Same as previous test but using ``async for`` syntax/api.

    """

    # signal to parent that we're up
    await ctx.started(data + 1)

    print('opening stream in callee')
    async with ctx.open_stream() as stream:

        count = 0
        async for msg in stream:

            assert msg == 'ping'
            print('pong')
            await stream.send('pong')
            count += 1

        else:
            assert count == 10


@pytest.mark.parametrize(
    'use_async_for',
    [True, False],
)
@pytest.mark.parametrize(
    'server_func',
    [simple_rpc, simple_rpc_with_forloop],
)
def test_simple_rpc(server_func, use_async_for):
    '''
    The simplest request response pattern.

    '''
    async def main():
        async with tractor.open_nursery() as n:

            portal = await n.start_actor(
                'rpc_server',
                enable_modules=[__name__],
            )

            async with portal.open_context(
                server_func,  # taken from pytest parameterization
                data=10,
            ) as (ctx, sent):

                assert sent == 11

                async with ctx.open_stream() as stream:

                    if use_async_for:

                        count = 0
                        # receive msgs using async for style
                        print('ping')
                        await stream.send('ping')

                        async for msg in stream:
                            assert msg == 'pong'
                            print('ping')
                            await stream.send('ping')
                            count += 1

                            if count >= 9:
                                break

                    else:
                        # classic send/receive style
                        for _ in range(10):

                            print('ping')
                            await stream.send('ping')
                            assert await stream.receive() == 'pong'

                # stream should terminate here

            # final context result(s) should be consumed here in __aexit__()

            await portal.cancel_actor()

    trio.run(main)
