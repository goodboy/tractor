"""
Actor model API testing
"""
import time
from functools import partial

import pytest
import trio
import tractor


@pytest.fixture
def us_symbols():
    return ['TSLA', 'AAPL', 'CGC', 'CRON']


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

                gen = await portal.run(
                    __name__,
                    'stream_seq',  # the func above
                    sequence=list(seq),  # has to be msgpack serializable
                )
                # it'd sure be nice to have an asyncitertools here...
                iseq = iter(seq)
                async for val in gen:
                    assert val == next(iseq)
                    break
                    # terminate far-end async-gen
                    # await gen.asend(None)
                    # break

                # stop all spawned subactors
                await nursery.cancel()


def test_stream_from_single_subactor(us_symbols):
    """Verify streaming from a spawned async generator.
    """
    tractor.run(
        stream_from_single_subactor,
        name='client',
    )


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
                raise
            except Exception:
                pass
            assert 0, "Remote error was not raised?"

    with pytest.raises(tractor.RemoteActorError):
        tractor.run(main)
