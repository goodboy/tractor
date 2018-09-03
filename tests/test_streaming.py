"""
Streaming via async gen api
"""
import time

import trio
import tractor
import pytest


async def stream_seq(sequence):
    for i in sequence:
        yield i
        await trio.sleep(0.1)


async def stream_from_single_subactor():
    """Verify we can spawn a daemon actor and retrieve streamed data.
    """
    async with tractor.find_actor('brokerd') as portals:
        if not portals:
            # only one per host address, spawns an actor if None
            async with tractor.open_nursery() as nursery:
                # no brokerd actor found
                portal = await nursery.start_actor(
                    'streamerd',
                    rpc_module_paths=[__name__],
                    statespace={'global_dict': {}},
                )

                seq = range(10)

                agen = await portal.run(
                    __name__,
                    'stream_seq',  # the func above
                    sequence=list(seq),  # has to be msgpack serializable
                )
                # it'd sure be nice to have an asyncitertools here...
                iseq = iter(seq)
                async for val in agen:
                    assert val == next(iseq)
                    # TODO: test breaking the loop (should it kill the
                    # far end?)
                    # break
                    # terminate far-end async-gen
                    # await gen.asend(None)
                    # break

                # stop all spawned subactors
                await portal.cancel_actor()
                # await nursery.cancel()


def test_stream_from_single_subactor(arb_addr):
    """Verify streaming from a spawned async generator.
    """
    tractor.run(stream_from_single_subactor, arbiter_addr=arb_addr)


# this is the first 2 actors, streamer_1 and streamer_2
async def stream_data(seed):
    for i in range(seed):
        yield i
        await trio.sleep(0)  # trigger scheduler


# this is the third actor; the aggregator
async def aggregate(seed):
    """Ensure that the two streams we receive match but only stream
    a single set of values to the parent.
    """
    async with tractor.open_nursery() as nursery:
        portals = []
        for i in range(1, 3):
            # fork point
            portal = await nursery.start_actor(
                name=f'streamer_{i}',
                rpc_module_paths=[__name__],
            )

            portals.append(portal)

        q = trio.Queue(500)

        async def push_to_q(portal):
            async for value in await portal.run(
                __name__, 'stream_data', seed=seed
            ):
                # leverage trio's built-in backpressure
                await q.put(value)

            await q.put(None)
            print(f"FINISHED ITERATING {portal.channel.uid}")

        # spawn 2 trio tasks to collect streams and push to a local queue
        async with trio.open_nursery() as n:
            for portal in portals:
                n.start_soon(push_to_q, portal)

            unique_vals = set()
            async for value in q:
                if value not in unique_vals:
                    unique_vals.add(value)
                    # yield upwards to the spawning parent actor
                    yield value

                    if value is None:
                        break

                assert value in unique_vals

            print("FINISHED ITERATING in aggregator")

        await nursery.cancel()
        print("WAITING on `ActorNursery` to finish")
    print("AGGREGATOR COMPLETE!")


# this is the main actor and *arbiter*
async def a_quadruple_example():
    # a nursery which spawns "actors"
    async with tractor.open_nursery() as nursery:

        seed = int(1e3)
        pre_start = time.time()

        portal = await nursery.run_in_actor(
            'aggregator',
            aggregate,
            seed=seed,
        )

        start = time.time()
        # the portal call returns exactly what you'd expect
        # as if the remote "aggregate" function was called locally
        result_stream = []
        async for value in await portal.result():
            result_stream.append(value)

        print(f"STREAM TIME = {time.time() - start}")
        print(f"STREAM + SPAWN TIME = {time.time() - pre_start}")
        assert result_stream == list(range(seed)) + [None]
        return result_stream


async def cancel_after(wait):
    with trio.move_on_after(wait):
        return await a_quadruple_example()


@pytest.fixture(scope='module')
def time_quad_ex(arb_addr):
    start = time.time()
    results = tractor.run(cancel_after, 3, arbiter_addr=arb_addr)
    diff = time.time() - start
    assert results
    return results, diff


def test_a_quadruple_example(time_quad_ex):
    """This also serves as a kind of "we'd like to be this fast test"."""
    results, diff = time_quad_ex
    assert results
    assert diff < 2.5


@pytest.mark.parametrize(
    'cancel_delay',
    list(map(lambda i: i/10, range(2, 8)))
)
def test_not_fast_enough_quad(arb_addr, time_quad_ex, cancel_delay):
    """Verify we can cancel midway through the quad example and all actors
    cancel gracefully.
    """
    results, diff = time_quad_ex
    delay = max(diff - cancel_delay, 0)
    results = tractor.run(cancel_after, delay, arbiter_addr=arb_addr)
    assert results is None
