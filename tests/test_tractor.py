"""
Actor model API testing
"""
import time
from functools import partial

import pytest
import trio
from piker import tractor


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
    # name = 'doggy-service'
    statespace = {'doggy': 10, 'kitty': 4}
    namespaces = ['piker.brokers.core']

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
            assert not portal._event.is_set()
            assert portal._uid in tractor.current_actor()._peers
    else:
        return 10


def test_local_arbiter_subactor_global_state():
    statespace = {'doggy': 10, 'kitty': 4}
    tractor.run(
        spawn,
        True,
        name='arbiter',
        statespace=statespace,
    )


@pytest.mark.trio
async def test_rx_price_quotes_from_brokerd(us_symbols):
    """Verify we can spawn a daemon actor and retrieve streamed price data.
    """
    async with tractor.find_actor('brokerd') as portals:
        if not portals:
            # only one per host address, spawns an actor if None
            async with tractor.open_nursery() as nursery:
                # no brokerd actor found
                portal = await nursery.start_actor(
                    'brokerd',
                    rpc_module_paths=['piker.brokers.core'],
                    statespace={
                        'brokers2tickersubs': {},
                        'clients': {},
                        'dtasks': set()
                    },
                    main=None,  # don't start a main func - use rpc
                )

                # gotta expose in a broker agnostic way...
                # retrieve initial symbol data
                # sd = await portal.run(
                #     'piker.brokers.core', 'symbol_data', symbols=us_symbols)
                # assert list(sd.keys()) == us_symbols

                gen = await portal.run(
                    'piker.brokers.core',
                    '_test_price_stream',
                    broker='robinhood',
                    symbols=us_symbols,
                )
                # it'd sure be nice to have an asyncitertools here...
                async for quotes in gen:
                    assert quotes
                    for key in quotes:
                        assert key in us_symbols
                    break
                    # terminate far-end async-gen
                    # await gen.asend(None)
                    # break

                # stop all spawned subactors
                await nursery.cancel()

    # arbitter is cancelled here due to `find_actors()` internals
    # (which internally uses `get_arbiter` which kills its channel
    # server scope on exit)
