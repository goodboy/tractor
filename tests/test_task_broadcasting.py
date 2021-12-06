"""
Broadcast channels for fan-out to local tasks.

"""
from contextlib import asynccontextmanager
from functools import partial
from itertools import cycle
import time
from typing import Optional, List, Tuple

import pytest
import trio
from trio.lowlevel import current_task
import tractor
from tractor.trionics import broadcast_receiver, Lagged


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
                await stream.send(value)
                print(f'producer sent {value}')


async def ensure_sequence(

    stream: tractor.ReceiveMsgStream,
    sequence: list,
    delay: Optional[float] = None,

) -> None:

    name = current_task().name
    async with stream.subscribe() as bcaster:
        assert not isinstance(bcaster, type(stream))
        async for value in bcaster:
            print(f'{name} rx: {value}')
            assert value == sequence[0]
            sequence.remove(value)

            if delay:
                await trio.sleep(delay)

            if not sequence:
                # fully consumed
                break


@asynccontextmanager
async def open_sequence_streamer(

    sequence: List[int],
    arb_addr: Tuple[str, int],
    start_method: str,

) -> tractor.MsgStream:

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
            async with ctx.open_stream(backpressure=True) as stream:
                yield stream

        await portal.cancel_actor()


def test_stream_fan_out_to_local_subscriptions(
    arb_addr,
    start_method,
):

    sequence = list(range(1000))

    async def main():

        async with open_sequence_streamer(
            sequence,
            arb_addr,
            start_method,
        ) as stream:

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

    trio.run(main)


@pytest.mark.parametrize(
    'task_delays',
    [
        (0.01, 0.001),
        (0.001, 0.01),
    ]
)
def test_consumer_and_parent_maybe_lag(
    arb_addr,
    start_method,
    task_delays,
):

    async def main():

        sequence = list(range(300))
        parent_delay, sub_delay = task_delays

        async with open_sequence_streamer(
            sequence,
            arb_addr,
            start_method,
        ) as stream:

            try:
                async with trio.open_nursery() as n:

                    n.start_soon(
                        ensure_sequence,
                        stream,
                        sequence.copy(),
                        sub_delay,
                        name='consumer_task',
                    )

                    await stream.send(tuple(sequence))

                    # async for value in stream:
                    lagged = False
                    lag_count = 0

                    while True:
                        try:
                            value = await stream.receive()
                            print(f'source stream rx: {value}')

                            if lagged:
                                # re set the sequence starting at our last
                                # value
                                sequence = sequence[sequence.index(value) + 1:]
                            else:
                                assert value == sequence[0]
                                sequence.remove(value)

                            lagged = False

                        except Lagged:
                            lagged = True
                            print(f'source stream lagged after {value}')
                            lag_count += 1
                            continue

                        # lag the parent
                        await trio.sleep(parent_delay)

                        if not sequence:
                            # fully consumed
                            break
                    print(f'parent + source stream lagged: {lag_count}')

                    if parent_delay > sub_delay:
                        assert lag_count > 0

            except Lagged:
                # child was lagged
                assert parent_delay < sub_delay

    trio.run(main)


def test_faster_task_to_recv_is_cancelled_by_slower(
    arb_addr,
    start_method,
):
    '''Ensure that if a faster task consuming from a stream is cancelled
    the slower task can continue to receive all expected values.

    '''
    async def main():

        sequence = list(range(1000))

        async with open_sequence_streamer(
            sequence,
            arb_addr,
            start_method,

        ) as stream:

            async with trio.open_nursery() as n:
                n.start_soon(
                    ensure_sequence,
                    stream,
                    sequence.copy(),
                    0,
                    name='consumer_task',
                )

                await stream.send(tuple(sequence))

                # pull 3 values, cancel the subtask, then
                # expect to be able to pull all values still
                for i in range(20):
                    try:
                        value = await stream.receive()
                        print(f'source stream rx: {value}')
                        await trio.sleep(0.01)
                    except Lagged:
                        print(f'parent overrun after {value}')
                        continue

                print('cancelling faster subtask')
                n.cancel_scope.cancel()

            try:
                value = await stream.receive()
                print(f'source stream after cancel: {value}')
            except Lagged:
                print(f'parent overrun after {value}')

            # expect to see all remaining values
            with trio.fail_after(0.5):
                async for value in stream:
                    assert stream._broadcaster._state.recv_ready is None
                    print(f'source stream rx: {value}')
                    if value == 999:
                        # fully consumed and we missed no values once
                        # the faster subtask was cancelled
                        break

                # await tractor.breakpoint()
                # await stream.receive()
                print(f'final value: {value}')

    trio.run(main)


def test_subscribe_errors_after_close():

    async def main():

        size = 1
        tx, rx = trio.open_memory_channel(size)
        async with broadcast_receiver(rx, size) as brx:
            pass

        try:
            # open and close
            async with brx.subscribe():
                pass

        except trio.ClosedResourceError:
            assert brx.key not in brx._state.subs

        else:
            assert 0

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
                        print(f'{task.name}: starting consume loop')
                        try:
                            async for value in lbrx:
                                print(f'{task.name}: {value}')
                                await trio.sleep(delay)

                            if task.name == 'sub_1':
                                # trigger checkpoint to clean out other subs
                                await trio.sleep(0.01)

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
                                f'restarting slow task {task.name} '
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
                # above, never was iterated and is thus overrun
                try:
                    await brx.receive()
                except Lagged:
                    # expect tokio style index truncation
                    seq = brx._state.subs[brx.key]
                    assert seq == len(brx._state.queue) - 1

                # all backpressured entries in the underlying
                # channel should have been copied into the caster
                # queue trailing-window
                async for i in rx:
                    print(f'bped: {i}')
                    assert i in brx._state.queue

                # should be noop
                await brx.aclose()

    trio.run(main)


def test_first_recver_is_cancelled():

    async def main():

        # make sure it all works within the runtime
        async with tractor.open_root_actor():

            tx, rx = trio.open_memory_channel(1)
            brx = broadcast_receiver(rx, 1)
            cs = trio.CancelScope()

            async def sub_and_recv():
                with cs:
                    async with brx.subscribe() as bc:
                        async for value in bc:
                            print(value)

            async def cancel_and_send():
                await trio.sleep(0.2)
                cs.cancel()
                await tx.send(1)

            async with trio.open_nursery() as n:

                n.start_soon(sub_and_recv)
                await trio.sleep(0.1)
                assert brx._state.recv_ready

                n.start_soon(cancel_and_send)

                # ensure that we don't hang because no-task is now
                # waiting on the underlying receive..
                with trio.fail_after(0.5):
                    value = await brx.receive()
                    print(f'parent: {value}')
                    assert value == 1

    trio.run(main)
