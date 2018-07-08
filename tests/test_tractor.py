"""
Actor model API testing
"""
import time
from functools import partial

import pytest
import trio
import tractor


@pytest.mark.trio
async def test_no_arbitter():
    """An arbitter must be established before any nurseries
    can be created.

    (In other words ``tractor.run`` must be used instead of ``trio.run`` as is
    done by the ``pytest-trio`` plugin.)
    """
    with pytest.raises(RuntimeError):
        with tractor.open_nursery():
            pass


def test_local_actor_async_func():
    """Verify a simple async function in-process.
    """
    nums = []

    async def print_loop():
        # arbiter is started in-proc if dne
        assert tractor.current_actor().is_arbiter

        for i in range(10):
            nums.append(i)
            await trio.sleep(0.1)

    start = time.time()
    tractor.run(print_loop)

    # ensure the sleeps were actually awaited
    assert time.time() - start >= 1
    assert nums == list(range(10))


# NOTE: this func must be defined at module level in order for the
# interal pickling infra of the forkserver to work
async def spawn(is_arbiter):
    statespace = {'doggy': 10, 'kitty': 4}
    namespaces = [__name__]

    await trio.sleep(0.1)
    actor = tractor.current_actor()
    assert actor.is_arbiter == is_arbiter

    # arbiter should always have an empty statespace as it's redundant
    assert actor.statespace == statespace

    if actor.is_arbiter:
        async with tractor.open_nursery() as nursery:
            # forks here
            portal = await nursery.start_actor(
                'sub-actor',
                main=partial(spawn, False),
                statespace=statespace,
                rpc_module_paths=namespaces,
            )
            assert len(nursery._children) == 1
            assert portal.channel.uid in tractor.current_actor()._peers
            # be sure we can still get the result
            result = await portal.result()
            assert result == 10
            return result
    else:
        return 10


def test_local_arbiter_subactor_global_state():
    statespace = {'doggy': 10, 'kitty': 4}
    result = tractor.run(
        spawn,
        True,
        name='arbiter',
        statespace=statespace,
    )
    assert result == 10


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
                    # don't start a main func - use rpc
                    # currently the same as outlive_main=False
                    main=None,
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


def test_stream_from_single_subactor():
    """Verify streaming from a spawned async generator.
    """
    tractor.run(stream_from_single_subactor)


async def assert_err():
    assert 0


def test_remote_error():
    """Verify an error raises in a subactor is propagated to the parent.
    """
    async def main():
        async with tractor.open_nursery() as nursery:

            portal = await nursery.start_actor('errorer', main=assert_err)

            # get result(s) from main task
            try:
                return await portal.result()
            except tractor.RemoteActorError:
                print("Look Maa that actor failed hard, hehh")
                raise
            except Exception:
                pass
            assert 0, "Remote error was not raised?"

    with pytest.raises(tractor.RemoteActorError):
        # also raises
        tractor.run(main)


def do_nothing():
    pass


def test_cancel_single_subactor():

    async def main():

        async with tractor.open_nursery() as nursery:

            portal = await nursery.start_actor(
                'nothin', rpc_module_paths=[__name__],
            )
            assert not await portal.run(__name__, 'do_nothing')

            # would hang otherwise
            await nursery.cancel()

    tractor.run(main)


async def stream_data(seed):
    for i in range(seed):
        yield i
        # await trio.sleep(1/10000.)  # trigger scheduler
        await trio.sleep(0)  # trigger scheduler


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
                outlive_main=True,  # daemonize these actors
            )

            portals.append(portal)

        q = trio.Queue(int(1e3))

        async def push_to_q(portal):
            async for value in await portal.run(
                __name__, 'stream_data', seed=seed
            ):
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
                    continue

                assert value in unique_vals
                if value is None:
                    break

            print("FINISHED ITERATING in aggregator")

        await nursery.cancel()
        print("WAITING on `ActorNursery` to finish")
    print("AGGREGATOR COMPLETE!")


async def main():
    # a nursery which spawns "actors"
    async with tractor.open_nursery() as nursery:

        seed = int(10)
        import time
        pre_start = time.time()
        portal = await nursery.start_actor(
            name='aggregator',
            # executed in the actor's "main task" immediately
            main=partial(aggregate, seed),
        )

        start = time.time()
        # the portal call returns exactly what you'd expect
        # as if the remote "main" function was called locally
        result_stream = []
        async for value in await portal.result():
            result_stream.append(value)

        print(f"STREAM TIME = {time.time() - start}")
        print(f"STREAM + SPAWN TIME = {time.time() - pre_start}")
        assert result_stream == list(range(seed)) + [None]


def test_show_me_the_code():
    """Verify the *show me the code* readme example works.
    """
    tractor.run(main, arbiter_addr=('127.0.0.1', 1616))


def test_cancel_smtc():
    """Verify we can cancel midway through the smtc example gracefully.
    """
    pass
