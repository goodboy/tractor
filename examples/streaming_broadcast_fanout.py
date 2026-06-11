'''
Demonstrate fanning out ONE inter-actor `MsgStream` to N
local (parent-side) trio tasks using `MsgStream.subscribe()`:
each subscriber gets its own `BroadcastReceiver` copy of
every msg from the single underlying IPC stream.

The child waits for a 'go' msg so that all subscribers are
guaranteed-attached before the first tick is sent; when the
child's stream closes each subscriber's `async for` ends
cleanly.

'''
import trio
import tractor


@tractor.context
async def tick_stream(
    ctx: tractor.Context,
    count: int,
) -> None:
    '''
    Send `count` "ticks" once the parent says go.

    '''
    await ctx.started(count)
    async with ctx.open_stream() as stream:
        # wait for the go-signal ensuring every parent-side
        # subscriber is attached before any tick is sent.
        assert await stream.receive() == 'go'
        for i in range(count):
            await stream.send(i)
        # falling out gracefully closes our stream side;
        # all parent-side subscribers see end-of-channel.


async def consume(
    name: str,
    stream: tractor.MsgStream,
    task_status: trio.TaskStatus = trio.TASK_STATUS_IGNORED,
) -> None:
    '''
    Consume a private broadcast-copy of the IPC stream.

    '''
    async with stream.subscribe() as bcaster:
        task_status.started()
        ticks: list[int] = []
        async for tick in bcaster:
            print(f'{name}: rx {tick}')
            ticks.append(tick)
        # EVERY subscriber gets its own full copy B)
        print(f'{name}: stream ended, got {ticks}')


async def main() -> None:
    async with tractor.open_nursery() as an:
        portal = await an.start_actor(
            'ticker',
            enable_modules=[__name__],
        )
        async with (
            portal.open_context(
                tick_stream,
                count=5,
            ) as (ctx, first),
            ctx.open_stream() as stream,
        ):
            assert first == 5
            async with trio.open_nursery() as tn:
                # use `.start()` so each consumer is known
                # to be subscribed before the ticks flow.
                for i in range(3):
                    await tn.start(
                        consume,
                        f'sub_{i}',
                        stream,
                    )
                await stream.send('go')
        await portal.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
