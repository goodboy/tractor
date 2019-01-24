import time
from functools import partial
from itertools import cycle

import pytest
import trio
import tractor
from async_generator import aclosing
from tractor.testing import tractor_test


def is_even(i):
    return i % 2 == 0


@tractor.msg.pub
async def pubber(get_topics):
    ss = tractor.current_actor().statespace

    for i in cycle(range(10)):

        # ensure topic subscriptions are as expected
        ss['get_topics'] = get_topics

        yield {'even' if is_even(i) else 'odd': i}
        await trio.sleep(0.1)


async def subs(which, pub_actor_name):
    if len(which) == 1:
        if which[0] == 'even':
            pred = is_even
        else:
            pred = lambda i: not is_even(i)
    else:
        pred = lambda i: isinstance(i, int)

    async with tractor.find_actor(pub_actor_name) as portal:
        agen = await portal.run(__name__, 'pubber', topics=which)
        async with aclosing(agen) as agen:
            async for pkt in agen:
                for topic, value in pkt.items():
                    assert pred(value)


@pytest.mark.parametrize(
    'pub_actor',
    ['streamer', 'arbiter']
)
def test_pubsub_multi_actor_subs(
    loglevel,
    arb_addr,
    pub_actor,
):
    """Try out the neato @pub decorator system.
    """
    async def main():
        ss = tractor.current_actor().statespace

        async with tractor.open_nursery() as n:

            name = 'arbiter'

            if pub_actor is 'streamer':
                # start the publisher as a daemon
                master_portal = await n.start_actor(
                    'streamer',
                    rpc_module_paths=[__name__],
                )

            even_portal = await n.run_in_actor(
                'evens', subs, which=['even'], pub_actor_name=name)
            odd_portal = await n.run_in_actor(
                'odds', subs, which=['odd'], pub_actor_name=name)

            async with tractor.wait_for_actor('evens'):
                # block until 2nd actor is initialized
                pass

            if pub_actor is 'arbiter':
                # wait for publisher task to be spawned in a local RPC task
                while not ss.get('get_topics'):
                    await trio.sleep(0.1)

                get_topics = ss.get('get_topics')

                assert 'even' in get_topics()

            async with tractor.wait_for_actor('odds'):
                # block until 2nd actor is initialized
                pass

            if pub_actor is 'arbiter':
                start = time.time()
                while 'odd' not in get_topics():
                    await trio.sleep(0.1)
                    if time.time() - start > 1:
                        pytest.fail("odds subscription never arrived?")

            # TODO: how to make this work when the arbiter gets
            # a portal to itself? Currently this causes a hang
            # when the channel server is torn down due to a lingering
            # loopback channel
            #     with trio.move_on_after(1):
            #         await subs(['even', 'odd'])

            # XXX: this would cause infinite
            # blocking due to actor never terminating loop
            # await even_portal.result()

            await trio.sleep(0.5)
            await even_portal.cancel_actor()
            await trio.sleep(0.5)

            if pub_actor is 'arbiter':
                assert 'even' not in get_topics()

            await odd_portal.cancel_actor()
            await trio.sleep(1)

            if pub_actor is 'arbiter':
                while get_topics():
                    await trio.sleep(0.1)
                    if time.time() - start > 1:
                        pytest.fail("odds subscription never dropped?")
            else:
                await master_portal.cancel_actor()

    tractor.run(
        main,
        arbiter_addr=arb_addr,
        rpc_module_paths=[__name__],
    )
