"""
Streaming via async gen api
"""
import time
from functools import partial
import platform

import trio
import tractor
import pytest


def test_must_define_ctx():

    with pytest.raises(TypeError) as err:
        @tractor.stream
        async def no_ctx():
            pass

    assert "no_ctx must be `ctx: tractor.Context" in str(err.value)

    @tractor.stream
    async def has_ctx(ctx):
        pass


async def async_gen_stream(sequence):
    for i in sequence:
        yield i
        await trio.sleep(0.1)

    # block indefinitely waiting to be cancelled by ``aclose()`` call
    with trio.CancelScope() as cs:
        await trio.sleep(float('inf'))
        assert 0
    assert cs.cancelled_caught


@tractor.stream
async def context_stream(ctx, sequence):
    for i in sequence:
        await ctx.send_yield(i)
        await trio.sleep(0.1)

    # block indefinitely waiting to be cancelled by ``aclose()`` call
    with trio.CancelScope() as cs:
        await trio.sleep(float('inf'))
        assert 0
    assert cs.cancelled_caught


async def stream_from_single_subactor(stream_func_name):
    """Verify we can spawn a daemon actor and retrieve streamed data.
    """
    async with tractor.find_actor('streamerd') as portals:
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

                stream = await portal.run(
                    __name__,
                    stream_func_name,  # one of the funcs above
                    sequence=list(seq),  # has to be msgpack serializable
                )
                # it'd sure be nice to have an asyncitertools here...
                iseq = iter(seq)
                ival = next(iseq)
                async for val in stream:
                    assert val == ival
                    try:
                        ival = next(iseq)
                    except StopIteration:
                        # should cancel far end task which will be
                        # caught and no error is raised
                        await stream.aclose()

                await trio.sleep(0.3)
                try:
                    await stream.__anext__()
                except StopAsyncIteration:
                    # stop all spawned subactors
                    await portal.cancel_actor()
                # await nursery.cancel()


@pytest.mark.parametrize(
    'stream_func', ['async_gen_stream', 'context_stream']
)
def test_stream_from_single_subactor(arb_addr, start_method, stream_func):
    """Verify streaming from a spawned async generator.
    """
    tractor.run(
        partial(
            stream_from_single_subactor,
            stream_func_name=stream_func,
        ),
        arbiter_addr=arb_addr,
        start_method=start_method,
    )


# this is the first 2 actors, streamer_1 and streamer_2
async def stream_data(seed):
    for i in range(seed):
        yield i
        # trigger scheduler to simulate practical usage
        await trio.sleep(0)


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

        send_chan, recv_chan = trio.open_memory_channel(500)

        async def push_to_chan(portal, send_chan):
            async with send_chan:
                async for value in await portal.run(
                    __name__, 'stream_data', seed=seed
                ):
                    # leverage trio's built-in backpressure
                    await send_chan.send(value)

            print(f"FINISHED ITERATING {portal.channel.uid}")

        # spawn 2 trio tasks to collect streams and push to a local queue
        async with trio.open_nursery() as n:

            for portal in portals:
                n.start_soon(push_to_chan, portal, send_chan.clone())

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
        assert result_stream == list(range(seed))
        return result_stream


async def cancel_after(wait):
    with trio.move_on_after(wait):
        return await a_quadruple_example()


@pytest.fixture(scope='module')
def time_quad_ex(arb_addr, ci_env, spawn_backend):
    if ci_env and spawn_backend == 'mp' and (platform.system() != 'Windows'):
        """no idea, but the travis and github actions, mp *nix runs are
        flaking out here often
        """
        pytest.skip("Test is too flaky on mp in CI")

    timeout = 7 if platform.system() in ('Windows', 'Darwin') else 4
    start = time.time()
    results = tractor.run(cancel_after, timeout, arbiter_addr=arb_addr)
    diff = time.time() - start
    assert results
    return results, diff


def test_a_quadruple_example(time_quad_ex, ci_env, spawn_backend):
    """This also serves as a kind of "we'd like to be this fast test"."""

    results, diff = time_quad_ex
    assert results
    this_fast = 6 if platform.system() in ('Windows', 'Darwin') else 2.5
    assert diff < this_fast


@pytest.mark.parametrize(
    'cancel_delay',
    list(map(lambda i: i/10, range(3, 9)))
)
def test_not_fast_enough_quad(
    arb_addr, time_quad_ex, cancel_delay, ci_env, spawn_backend
):
    """Verify we can cancel midway through the quad example and all actors
    cancel gracefully.
    """
    results, diff = time_quad_ex
    delay = max(diff - cancel_delay, 0)
    results = tractor.run(cancel_after, delay, arbiter_addr=arb_addr)
    if platform.system() == 'Windows' and results is not None:
        # In Windows CI it seems later runs are quicker then the first
        # so just ignore these
        print("Woa there windows caught your breath eh?")
    else:
        # should be cancelled mid-streaming
        assert results is None
