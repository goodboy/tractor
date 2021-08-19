"""
Broadcast channels for fan-out to local tasks.
"""
from functools import partial
from itertools import cycle
import time

import trio
from trio.lowlevel import current_task
import tractor
from tractor._broadcast import broadcast_receiver, Lagged


@tractor.context
async def echo_sequences(

    ctx:  tractor.Context,

) -> None:
    '''Bidir streaming endpoint which will stream
    back any sequence it is sent item-wise.

    '''
    await ctx.started()

    async with ctx.open_stream() as stream:
        async for sequence in stream:
            seq = list(sequence)
            for value in seq:
                print(f'sending {value}')
                await stream.send(value)


async def ensure_sequence(
    stream: tractor.ReceiveMsgStream,
    sequence: list,
) -> None:

    name = current_task().name
    async with stream.subscribe() as bcaster:
        assert not isinstance(bcaster, type(stream))
        async for value in bcaster:
            print(f'{name} rx: {value}')
            assert value == sequence[0]
            sequence.remove(value)

            if not sequence:
                # fully consumed
                break


def test_stream_fan_out_to_local_subscriptions(
    arb_addr,
    start_method,
):

    sequence = list(range(1000))

    async def main():

        async with tractor.open_nursery(
            arbiter_addr=arb_addr,
            start_method=start_method,
        ) as tn:

            portal = await tn.start_actor(
                'sequence_echoer',
                enable_modules=[__name__],
            )

            async with portal.open_context(
                echo_sequences,
            ) as (ctx, first):

                assert first is None
                async with ctx.open_stream() as stream:

                    async with trio.open_nursery() as n:
                        for i in range(10):
                            n.start_soon(
                                ensure_sequence,
                                stream,
                                sequence.copy(),
                                name=f'consumer_{i}',
                            )

                        await stream.send(tuple(sequence))

                        async for value in stream:
                            print(f'source stream rx: {value}')
                            assert value == sequence[0]
                            sequence.remove(value)

                            if not sequence:
                                # fully consumed
                                break

            await portal.cancel_actor()

    trio.run(main)


def test_ensure_slow_consumers_lag_out(
    arb_addr,
    start_method,
):
    '''This is a pure local task test; no tractor
    machinery is really required.

    '''
    async def main():

        # make sure it all works within the runtime
        async with tractor.open_root_actor():

            num_laggers = 4
            laggers: dict[str, int] = {}
            retries = 3
            size = 100
            tx, rx = trio.open_memory_channel(size)
            brx = broadcast_receiver(rx, size)

            async def sub_and_print(
                delay: float,
            ) -> None:

                task = current_task()
                start = time.time()

                async with brx.subscribe() as lbrx:
                    while True:
                        #     await tractor.breakpoint()
                        print(f'{task.name}: starting consume loop')
                        try:
                            async for value in lbrx:
                                print(f'{task.name}: {value}')
                                await trio.sleep(delay)

                            if task.name == 'sub_1':
                                # the non-lagger got
                                # a ``trio.EndOfChannel``
                                # because the ``tx`` below was closed
                                assert len(lbrx._state.subs) == 1

                                await lbrx.aclose()

                                assert len(lbrx._state.subs) == 0

                        except trio.ClosedResourceError:
                            # only the fast sub will try to re-enter
                            # iteration on the now closed bcaster
                            assert task.name == 'sub_1'
                            return

                        except Lagged:
                            lag_time = time.time() - start
                            lags = laggers[task.name]
                            print(
                                f'restarting slow ass {task.name} '
                                f'that bailed out on {lags}:{value} '
                                f'after {lag_time:.3f}')
                            if lags <= retries:
                                laggers[task.name] += 1
                                continue
                            else:
                                print(
                                    f'{task.name} was too slow and terminated '
                                    f'on {lags}:{value}')
                                return

            async with trio.open_nursery() as nursery:

                for i in range(1, num_laggers):

                    task_name = f'sub_{i}'
                    laggers[task_name] = 0
                    nursery.start_soon(
                        partial(
                            sub_and_print,
                            delay=i*0.001,
                        ),
                        name=task_name,
                    )

                # allow subs to sched
                await trio.sleep(0.1)

                async with tx:
                    for i in cycle(range(size)):
                        await tx.send(i)
                        if len(brx._state.subs) == 2:
                            # only one, the non lagger, sub is left
                            break

                # the non-lagger
                assert laggers.pop('sub_1') == 0

                for n, v in laggers.items():
                    assert v == 4

                assert tx._closed
                assert not tx._state.open_send_channels

                # check that "first" bcaster that we created
                # above, never wass iterated and is thus overrun
                try:
                    await brx.receive()
                except Lagged:
                    # expect tokio style index truncation
                    assert brx._state.subs[brx.key] == len(brx._state.queue) - 1

                # all backpressured entries in the underlying
                # channel should have been copied into the caster
                # queue trailing-window
                async for i in rx:
                    print(f'bped: {i}')
                    assert i in brx._state.queue

                # should be noop
                await brx.aclose()

    trio.run(main)
