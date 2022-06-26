import trio
import click
import tractor
import pydantic
# from multiprocessing import shared_memory


@tractor.context
async def just_sleep(

    ctx: tractor.Context,
    **kwargs,

) -> None:
    '''
    Test a small ping-pong 2-way streaming server.

    '''
    await ctx.started()
    await trio.sleep_forever()


async def main() -> None:

    proc = await trio.open_process( (
        'python',
        '-c',
        'import trio; trio.run(trio.sleep_forever)',
    ))
    await proc.wait()
    # await trio.sleep_forever()
    # async with tractor.open_nursery() as n:

    #     portal = await n.start_actor(
    #         'rpc_server',
    #         enable_modules=[__name__],
    #     )

    #     async with portal.open_context(
    #         just_sleep,  # taken from pytest parameterization
    #     ) as (ctx, sent):
    #         await trio.sleep_forever()



if __name__ == '__main__':
    import time
    # time.sleep(999)
    trio.run(main)
