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
    for i in cycle(range(10)):
        topics = get_topics()
        yield {'even' if is_even(i) else 'odd': i}
        await trio.sleep(0.1)


async def subs(which):
    if len(which) == 1:
        if which[0] == 'even':
            pred = is_even
        else:
            pred = lambda i: not is_even(i)
    else:
        pred = lambda i: isinstance(i, int)

    async with tractor.find_actor('streamer') as portal:
        agen = await portal.run(__name__, 'pubber', topics=which)
        async with aclosing(agen) as agen:
            async for pkt in agen:
                for topic, value in pkt.items():
                    assert pred(value)


def test_pubsub_multi_actor_subs(
    loglevel,
    arb_addr,
):
    async def main():
        async with tractor.open_nursery() as n:
            # start the publisher as a daemon
            master_portal = await n.start_actor(
                'streamer',
                rpc_module_paths=[__name__],
            )

            even_portal = await n.run_in_actor('evens', subs, which=['even'])
            odd_portal = await n.run_in_actor('odds', subs, which=['odd'])

            async with tractor.wait_for_actor('odds'):
                # block until 2nd actor is initialized
                pass

            # TODO: how to make this work when the arbiter gets
            # a portal to itself? Currently this causes a hang
            # when the channel server is torn down due to a lingering
            # loopback channel
            #     with trio.move_on_after(1):
            #         await subs(['even', 'odd'])

            # XXX: this would cause infinite
            # blocking due to actor never terminating loop
            # await even_portal.result()

            await trio.sleep(1)
            await even_portal.cancel_actor()
            await trio.sleep(1)
            await odd_portal.cancel_actor()

            await master_portal.cancel_actor()

    tractor.run(
        main,
        arbiter_addr=arb_addr,
        rpc_module_paths=[__name__],
    )
