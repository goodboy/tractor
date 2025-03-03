'''
Advanced streaming patterns using bidirectional streams and contexts.

'''
from collections import Counter
import itertools
import platform

import pytest
import trio
import tractor


def is_win():
    return platform.system() == 'Windows'


_registry: dict[str, set[tractor.MsgStream]] = {
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

        for sub_stream in _registry[sub].copy():
            await sub_stream.send(val)

        # throttle send rate to ~1kHz
        # making it readable to a human user
        await trio.sleep(1/1000)


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

    subs: list[str],

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
            with trio.fail_after(3):
                await trio.sleep_forever()

    try:
        trio.run(main)
    except (
        trio.TooSlowError,
        ExceptionGroup,
    ) as err:
        if isinstance(err, ExceptionGroup):
            for suberr in err.exceptions:
                if isinstance(suberr, trio.TooSlowError):
                    break
            else:
                pytest.fail('Never got a `TooSlowError` ?')


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
    '''
    Test a subactor that both streams with one task and
    spawns another which handles a small requests-response
    dialogue over the same bidir-stream.

    '''
    async def main():

        # flat to make sure we get at least one pong
        got_pong: bool = False
        timeout: int = 2

        if is_win():  # smh
            timeout = 4

        with trio.move_on_after(timeout):
            async with tractor.open_nursery() as n:

                # name of this actor will be same as target func
                portal = await n.start_actor(
                    'dual_tasks',
                    enable_modules=[__name__]
                )

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


async def async_gen_stream(sequence):
    for i in sequence:
        yield i
        await trio.sleep(0.1)


@tractor.context
async def echo_ctx_stream(
    ctx: tractor.Context,
) -> None:
    await ctx.started()

    async with ctx.open_stream() as stream:
        async for msg in stream:
            await stream.send(msg)


def test_sigint_both_stream_types():
    '''Verify that running a bi-directional and recv only stream
    side-by-side will cancel correctly from SIGINT.

    '''
    timeout: float = 2
    if is_win():  # smh
        timeout += 1

    async def main():
        with trio.fail_after(timeout):
            async with tractor.open_nursery() as n:
                # name of this actor will be same as target func
                portal = await n.start_actor(
                    '2_way',
                    enable_modules=[__name__]
                )

                async with portal.open_context(echo_ctx_stream) as (ctx, _):
                    async with ctx.open_stream() as stream:
                        async with portal.open_stream_from(
                            async_gen_stream,
                            sequence=list(range(1)),
                        ) as gen_stream:

                            msg = await gen_stream.receive()
                            await stream.send(msg)
                            resp = await stream.receive()
                            assert resp == msg
                            raise KeyboardInterrupt

    try:
        trio.run(main)
        assert 0, "Didn't receive KBI!?"
    except KeyboardInterrupt:
        pass


@tractor.context
async def inf_streamer(
    ctx: tractor.Context,

) -> None:
    '''
    Stream increasing ints until terminated with a 'done' msg.

    '''
    await ctx.started()

    async with (
        ctx.open_stream() as stream,

        # XXX TODO, INTERESTING CASE!!
        # - if we don't collapse the eg then the embedded
        # `trio.EndOfChannel` doesn't propagate directly to the above
        # .open_stream() parent, resulting in it also raising instead
        # of gracefully absorbing as normal.. so how to handle?
        trio.open_nursery(
            strict_exception_groups=False,
        ) as tn,
    ):
        async def close_stream_on_sentinel():
            async for msg in stream:
                if msg == 'done':
                    print(
                        'streamer RXed "done" sentinel msg!\n'
                        'CLOSING `MsgStream`!'
                    )
                    await stream.aclose()
                else:
                    print(f'streamer received {msg}')
            else:
                print('streamer exited recv loop')

        # start termination detector
        tn.start_soon(close_stream_on_sentinel)

        cap: int = 10000  # so that we don't spin forever when bug..
        for val in range(cap):
            try:
                print(f'streamer sending {val}')
                await stream.send(val)
                if val > cap:
                    raise RuntimeError(
                        'Streamer never cancelled by setinel?'
                    )
                await trio.sleep(0.001)

            # close out the stream gracefully
            except trio.ClosedResourceError:
                print('transport closed on streamer side!')
                assert stream.closed
                break
        else:
            raise RuntimeError(
                'Streamer not cancelled before finished sending?'
            )

    print('streamer exited .open_streamer() block')


def test_local_task_fanout_from_stream(
    debug_mode: bool,
):
    '''
    Single stream with multiple local consumer tasks using the
    ``MsgStream.subscribe()` api.

    Ensure all tasks receive all values after stream completes
    sending.

    '''
    consumers: int = 22

    async def main():

        counts = Counter()

        async with tractor.open_nursery(
            debug_mode=debug_mode,
        ) as tn:
            p: tractor.Portal = await tn.start_actor(
                'inf_streamer',
                enable_modules=[__name__],
            )
            async with (
                p.open_context(inf_streamer) as (ctx, _),
                ctx.open_stream() as stream,
            ):
                async def pull_and_count(name: str):
                    # name = trio.lowlevel.current_task().name
                    async with stream.subscribe() as recver:
                        assert isinstance(
                            recver,
                            tractor.trionics.BroadcastReceiver
                        )
                        async for val in recver:
                            print(f'bx {name} rx: {val}')
                            counts[name] += 1

                        print(f'{name} bcaster ended')

                    print(f'{name} completed')

                with trio.fail_after(3):
                    async with trio.open_nursery() as nurse:
                        for i in range(consumers):
                            nurse.start_soon(
                                pull_and_count,
                                i,
                            )

                        # delay to let bcast consumers pull msgs
                        await trio.sleep(0.5)
                        print('terminating nursery of bcast rxer consumers!')
                        await stream.send('done')

            print('closed stream connection')

            assert len(counts) == consumers
            mx = max(counts.values())
            # make sure each task received all stream values
            assert all(val == mx for val in counts.values())

            await p.cancel_actor()

    trio.run(main)
