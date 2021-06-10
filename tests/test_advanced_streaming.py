"""
Advanced streaming patterns using bidirectional streams and contexts.

"""
import itertools
from typing import Set, Dict, List

import trio
import tractor


_registry: Dict[str, Set[tractor.ReceiveMsgStream]] = {
    'even': set(),
    'odd': set(),
}


async def publisher(

    seed: int = 0,

) -> None:

    global _registry

    def is_even(i):
        return i % 2 == 0

    for val in itertools.count(seed):

        sub = 'even' if is_even(val) else 'odd'

        for sub_stream in _registry[sub]:
            await sub_stream.send(val)

        # throttle send rate to ~4Hz
        # making it readable to a human user
        await trio.sleep(1/4)


@tractor.context
async def subscribe(

    ctx: tractor.Context,

) -> None:

    global _registry

    # syn caller
    await ctx.started(None)

    async with ctx.open_stream() as stream:

        # update subs list as consumer requests
        async for new_subs in stream:

            new_subs = set(new_subs)
            remove = new_subs - _registry.keys()

            print(f'setting sub to {new_subs} for {ctx.chan.uid}')

            # remove old subs
            for sub in remove:
                _registry[sub].remove(stream)

            # add new subs for consumer
            for sub in new_subs:
                _registry[sub].add(stream)


async def consumer(

    subs: List[str],

) -> None:

    uid = tractor.current_actor().uid

    async with tractor.wait_for_actor('publisher') as portal:
        async with portal.open_context(subscribe) as (ctx, first):
            async with ctx.open_stream() as stream:

                # flip between the provided subs dynamically
                if len(subs) > 1:

                    for sub in itertools.cycle(subs):
                        print(f'setting dynamic sub to {sub}')
                        await stream.send([sub])

                        count = 0
                        async for value in stream:
                            print(f'{uid} got: {value}')
                            if count > 5:
                                break
                            count += 1

                else:  # static sub

                    await stream.send(subs)
                    async for value in stream:
                        print(f'{uid} got: {value}')


def test_dynamic_pub_sub():

    global _registry

    from multiprocessing import cpu_count
    cpus = cpu_count()

    async def main():
        async with tractor.open_nursery() as n:

            # name of this actor will be same as target func
            await n.run_in_actor(publisher)

            for i, sub in zip(
                range(cpus - 2),
                itertools.cycle(_registry.keys())
            ):
                await n.run_in_actor(
                    consumer,
                    name=f'consumer_{sub}',
                    subs=[sub],
                )

            # make one dynamic subscriber
            await n.run_in_actor(
                consumer,
                name='consumer_dynamic',
                subs=list(_registry.keys()),
            )

            # block until cancelled by user
            with trio.fail_after(10):
                await trio.sleep_forever()

    try:
        trio.run(main)
    except trio.TooSlowError:
        pass


@tractor.context
async def one_task_streams_and_one_handles_reqresp(

    ctx: tractor.Context,

) -> None:

    await ctx.started()

    async with ctx.open_stream() as stream:

        async def pingpong():
            '''Run a simple req/response service.

            '''
            async for msg in stream:
                print('rpc server ping')
                assert msg == 'ping'
                print('rpc server pong')
                await stream.send('pong')

        async with trio.open_nursery() as n:
            n.start_soon(pingpong)

            for _ in itertools.count():
                await stream.send('yo')
                await trio.sleep(0.01)


def test_reqresp_ontopof_streaming():
    '''Test a subactor that both streams with one task and
    spawns another which handles a small requests-response
    dialogue over the same bidir-stream.

    '''
    async def main():

        with trio.move_on_after(2):
            async with tractor.open_nursery() as n:

                # name of this actor will be same as target func
                portal = await n.start_actor(
                    'dual_tasks',
                    enable_modules=[__name__]
                )

                # flat to make sure we get at least one pong
                got_pong: bool = False

                async with portal.open_context(
                    one_task_streams_and_one_handles_reqresp,

                ) as (ctx, first):

                    assert first is None

                    async with ctx.open_stream() as stream:

                        await stream.send('ping')

                        async for msg in stream:
                            print(f'client received: {msg}')

                            assert msg in {'pong', 'yo'}

                            if msg == 'pong':
                                got_pong = True
                                await stream.send('ping')
                                print('client sent ping')

        assert got_pong

    try:
        trio.run(main)
    except trio.TooSlowError:
        pass
