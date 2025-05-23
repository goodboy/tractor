import time
import trio
import tractor
from tractor import (
    ActorNursery,
    MsgStream,
    Portal,
)


# this is the first 2 actors, streamer_1 and streamer_2
async def stream_data(seed):
    for i in range(seed):
        yield i
        await trio.sleep(0.0001)  # trigger scheduler


# this is the third actor; the aggregator
async def aggregate(seed):
    '''
    Ensure that the two streams we receive match but only stream
    a single set of values to the parent.

    '''
    an: ActorNursery
    async with tractor.open_nursery() as an:
        portals: list[Portal] = []
        for i in range(1, 3):

            # fork/spawn call
            portal = await an.start_actor(
                name=f'streamer_{i}',
                enable_modules=[__name__],
            )

            portals.append(portal)

        send_chan, recv_chan = trio.open_memory_channel(500)

        async def push_to_chan(portal, send_chan):

            # TODO: https://github.com/goodboy/tractor/issues/207
            async with send_chan:
                async with portal.open_stream_from(stream_data, seed=seed) as stream:
                    async for value in stream:
                        # leverage trio's built-in backpressure
                        await send_chan.send(value)

            print(f"FINISHED ITERATING {portal.channel.uid}")

        # spawn 2 trio tasks to collect streams and push to a local queue
        async with trio.open_nursery() as n:

            for portal in portals:
                n.start_soon(
                    push_to_chan,
                    portal,
                    send_chan.clone(),
                )

            # close this local task's reference to send side
            await send_chan.aclose()

            unique_vals = set()
            async with recv_chan:
                async for value in recv_chan:
                    if value not in unique_vals:
                        unique_vals.add(value)
                        # yield upwards to the spawning parent actor
                        yield value

                assert value in unique_vals

            print("FINISHED ITERATING in aggregator")

        await an.cancel()
        print("WAITING on `ActorNursery` to finish")
    print("AGGREGATOR COMPLETE!")


async def main() -> list[int]:
    '''
    This is the "root" actor's main task's entrypoint.

    By default (and if not otherwise specified) that root process
    also acts as a "registry actor" / "registrar" on the localhost
    for the purposes of multi-actor "service discovery".

    '''
    # yes, a nursery which spawns `trio`-"actors" B)
    an: ActorNursery
    async with tractor.open_nursery(
        loglevel='cancel',
        # debug_mode=True,
    ) as an:

        seed = int(1e3)
        pre_start = time.time()

        portal: Portal = await an.start_actor(
            name='aggregator',
            enable_modules=[__name__],
        )

        stream: MsgStream
        async with portal.open_stream_from(
            aggregate,
            seed=seed,
        ) as stream:

            start = time.time()
            # the portal call returns exactly what you'd expect
            # as if the remote "aggregate" function was called locally
            result_stream: list[int] = []
            async for value in stream:
                result_stream.append(value)

        cancelled: bool = await portal.cancel_actor()
        assert cancelled

        print(f"STREAM TIME = {time.time() - start}")
        print(f"STREAM + SPAWN TIME = {time.time() - pre_start}")
        assert result_stream == list(range(seed))
        return result_stream


if __name__ == '__main__':
    final_stream = trio.run(main)
