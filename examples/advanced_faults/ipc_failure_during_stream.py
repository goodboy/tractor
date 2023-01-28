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
    break_ipc: bool = False,
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
        async for i in stream:
            print(f'child echoing {i}')
            await stream.send(i)
            if (
                break_ipc
                and i > 500
            ):
                '#################################\n'
                'Simulating child-side IPC BREAK!\n'
                '#################################'
                n.start_soon(break_channel_silently_then_error, stream)
                n.start_soon(close_stream_and_error, stream)


async def main(
    debug_mode: bool = False,
    start_method: str = 'trio',
    break_parent_ipc: bool = False,
    break_child_ipc: bool = False,

) -> None:

    async with (
        open_nursery(
            start_method=start_method,

            # NOTE: even debugger is used we shouldn't get
            # a hang since it never engages due to broken IPC
            debug_mode=debug_mode,
            loglevel='warning',

        ) as an,
    ):
        portal = await an.start_actor(
            'chitty_hijo',
            enable_modules=[__name__],
        )

        async with portal.open_context(
            recv_and_spawn_net_killers,
            break_ipc=break_child_ipc,

        ) as (ctx, sent):
            async with ctx.open_stream() as stream:
                for i in range(1000):

                    if (
                        break_parent_ipc
                        and i > 100
                    ):
                        print(
                            '#################################\n'
                            'Simulating parent-side IPC BREAK!\n'
                            '#################################'
                        )
                        await stream._ctx.chan.send(None)

                    # it actually breaks right here in the
                    # mp_spawn/forkserver backends and thus the zombie
                    # reaper never even kicks in?
                    print(f'parent sending {i}')
                    await stream.send(i)

                    with trio.move_on_after(2) as cs:

                        # NOTE: in the parent side IPC failure case this
                        # will raise an ``EndOfChannel`` after the child
                        # is killed and sends a stop msg back to it's
                        # caller/this-parent.
                        rx = await stream.receive()

                        print(f"I'm a happy user and echoed to me is {rx}")

                    if cs.cancelled_caught:
                        # pretend to be a user seeing no streaming action
                        # thinking it's a hang, and then hitting ctl-c..
                        print("YOO i'm a user anddd thingz hangin..")

                print(
                    "YOO i'm mad send side dun but thingz hangin..\n"
                    'MASHING CTlR-C Ctl-c..'
                )
                raise KeyboardInterrupt


if __name__ == '__main__':
    trio.run(main)
