'''
Complex edge case where during real-time streaming the IPC tranport
channels are wiped out (purposely in this example though it could have
been an outage) and we want to ensure that despite being in debug mode
(or not) the user can sent SIGINT once they notice the hang and the
actor tree will eventually be cancelled without leaving any zombies.

'''
import trio
from tractor import (
    open_nursery,
    context,
    Context,
    MsgStream,
)


async def break_channel_silently_then_error(
    stream: MsgStream,
):
    async for msg in stream:
        await stream.send(msg)

        # XXX: close the channel right after an error is raised
        # purposely breaking the IPC transport to make sure the parent
        # doesn't get stuck in debug or hang on the connection join.
        # this more or less simulates an infinite msg-receive hang on
        # the other end.
        # if msg > 66:
        await stream._ctx.chan.send(None)
        assert 0


async def close_stream_and_error(
    stream: MsgStream,
):
    async for msg in stream:
        await stream.send(msg)

        # wipe out channel right before raising
        await stream._ctx.chan.send(None)
        await stream.aclose()
        assert 0


@context
async def recv_and_spawn_net_killers(

    ctx: Context,
    **kwargs,

) -> None:
    '''
    Receive stream msgs and spawn some IPC killers mid-stream.

    '''
    await ctx.started()
    async with (
        ctx.open_stream() as stream,
        trio.open_nursery() as n,
    ):
        for i in range(100):
            await stream.send(i)
            if i > 80:
                n.start_soon(break_channel_silently_then_error, stream)
                n.start_soon(close_stream_and_error, stream)
                await trio.sleep_forever()


async def main(
    debug_mode: bool = False,
    start_method: str = 'trio',

) -> None:

    async with open_nursery(
        start_method=start_method,

        # NOTE: even debugger is used we shouldn't get
        # a hang since it never engages due to broken IPC
        debug_mode=debug_mode,

    ) as n:
        portal = await n.start_actor(
            'chitty_hijo',
            enable_modules=[__name__],
        )

        async with portal.open_context(
            recv_and_spawn_net_killers,
        ) as (ctx, sent):
            async with ctx.open_stream() as stream:
                for i in range(100):

                    # this may break in the mp_spawn case
                    await stream.send(i)

                    with trio.move_on_after(2) as cs:
                        rx = await stream.receive()
                        print(f'I a mad user and here is what i got {rx}')

                    if cs.cancelled_caught:
                        # pretend to be a user seeing no streaming action
                        # thinking it's a hang, and then hitting ctl-c..
                        print("YOO i'm a user and, thingz hangin.. CTL-C CTRL-C..")
                        raise KeyboardInterrupt


if __name__ == '__main__':
    trio.run(main)
