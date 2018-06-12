"""
Actor model API testing
"""
import time

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
        with tractor.open_nursery() as nursery:
            pass


def test_local_actor_async_gen():
    """Verify a simple async function in-process.
    """
    async def print_loop(actor):
        for i in range(10):
            await trio.sleep(0.1)

    start = time.time()
    tractor.run(print_loop)
    # ensure the sleeps were actually awaited
    assert time.time() - start >= 1


@pytest.mark.trio
async def test_rx_price_quotes_from_brokerd(us_symbols):
    """Verify we can spawn a daemon actor and retrieve streamed price data.
    """
    async with tractor.find_actor('brokerd') as portals:
        async with tractor.open_nursery() as nursery:
            # only one per host address, spawns an actor if None
            if not portals:
                # no brokerd actor found
                portal = await nursery.start_actor(
                    'brokerd',
                    ['piker.brokers.core'],
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

        # arbitter is cancelled here due to `find_actors()` internals
        # (which internally uses `get_arbiter` which kills the root
        # nursery on __exit__)
