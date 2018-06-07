"""
Actor model API testing
"""
import pytest
from piker import tractor


@pytest.fixture
def us_symbols():
    return ['TSLA', 'AAPL', 'CGC', 'CRON']


@pytest.mark.trio
async def test_rx_price_quotes_from_brokerd(us_symbols):
    """Verify we can spawn a daemon actor and retrieve streamed price data.
    """
    async with tractor.open_nursery() as nursery:

        # only one per host address, spawns an actor if None
        async with tractor.find_actors('brokerd') as portals:
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
                    main=None,
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

        await nursery.cancel()
        # arbitter is cancelled here due to `find_actors()` internals
