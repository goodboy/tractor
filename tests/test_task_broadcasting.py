"""
Broadcast channels for fan-out to local tasks.

"""
from contextlib import (
    asynccontextmanager as acm,
)
from functools import partial
from itertools import cycle
import time
from types import SimpleNamespace
from typing import Optional
import warnings

import pytest
import trio
from trio.lowlevel import current_task
import tractor
from tractor.to_asyncio import LinkedTaskChannel
from tractor.trionics import (
    broadcast_receiver,
    BroadcastReceiveError,
    Lagged,
    collapse_eg,
)


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

    stream: tractor.MsgStream,
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


@acm
async def open_sequence_streamer(

    sequence: list[int],
    reg_addr: tuple[str, int],
    start_method: str,

) -> tractor.MsgStream:

    async with tractor.open_nursery(
        registry_addrs=[reg_addr],
        start_method=start_method,
    ) as an:

        portal = await an.start_actor(
            'sequence_echoer',
            enable_modules=[__name__],
        )

        async with portal.open_context(
            echo_sequences,
        ) as (ctx, first):

            assert first is None
            async with ctx.open_stream(allow_overruns=True) as stream:
                yield stream

        await portal.cancel_actor()


def test_stream_fan_out_to_local_subscriptions(
    reg_addr,
    start_method,
):

    sequence = list(range(1000))

    async def main():

        async with open_sequence_streamer(
            sequence,
            reg_addr,
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
    reg_addr,
    start_method,
    task_delays,
):

    async def main():

        sequence = list(range(300))
        parent_delay, sub_delay = task_delays

        async with open_sequence_streamer(
            sequence,
            reg_addr,
            start_method,
        ) as stream:

            try:
                async with (
                    collapse_eg(),
                    trio.open_nursery() as tn,
                ):

                    tn.start_soon(
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
    reg_addr,
    start_method,
):
    '''
    Ensure that if a faster task consuming from a stream is cancelled
    the slower task can continue to receive all expected values.

    '''
    async def main():

        sequence = list(range(1000))

        async with open_sequence_streamer(
            sequence,
            reg_addr,
            start_method,

        ) as stream:

            async with trio.open_nursery() as tn:
                tn.start_soon(
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
                tn.cancel_scope.cancel()

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

                # await tractor.pause()
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


@pytest.mark.parametrize(
    ('size', 'sent', 'dropped'),
    [
        (1, 2, 1),
        (3, 5, 2),
    ],
)
def test_lagged_reports_exact_drop_count(
    size: int,
    sent: int,
    dropped: int,
) -> None:
    '''
    `Lagged` must report every value outside the retained window.

    `BroadcastReceiver.receive_nowait()` previously subtracted the
    queue length from an already-invalid deque index without counting
    that first displaced value. A one-slot queue therefore claimed it
    dropped zero values after two sends. Keep one root receiver idle
    while a child subscriber drains every produced value, then prove
    the lag error reports the exact overrun and positions the root at
    the oldest value still retained by `BroadcastState.queue`.

    '''
    async def main() -> None:
        tx, rx = trio.open_memory_channel(size)
        brx = broadcast_receiver(rx, size)

        async with brx.subscribe() as fast:
            for value in range(sent):
                await tx.send(value)
                assert await fast.receive() == value

            match = rf'dropped `{dropped}` values'
            with pytest.raises(Lagged, match=match):
                await brx.receive()

            assert await brx.receive() == sent - size

    trio.run(main)


def test_broadcast_statistics_report_queued_counts() -> None:
    '''
    `BroadcastState.statistics()` must report counts, not indexes.

    Each `BroadcastState.subs` value is the deque index of a
    receiver's next unread value, with `-1` meaning caught up. The
    statistics API returned these indexes directly, so one queued
    value appeared as zero and every positive count was one short.
    Keep one root receiver idle while a child synchronously receives
    four produced values. Prove the root count advances through one
    and three retained values, then remains clamped to the three-slot
    retention window after lagging.

    Finally install an actual unwaited `trio.Event` in
    `BroadcastState.recv_ready` while treating deprecations as errors.
    This proves statistics checks `None` explicitly instead of using
    deprecated `trio.Event` truthiness.

    '''
    async def main() -> None:
        tx, rx = trio.open_memory_channel(3)
        brx = broadcast_receiver(rx, 3)

        async with brx.subscribe() as child:
            state = brx._state
            assert state.statistics()['queued_len_by_task'] == {
                brx.key: 0,
                child.key: 0,
            }

            await tx.send(0)
            assert await child.receive() == 0
            assert state.statistics()['queued_len_by_task'] == {
                brx.key: 1,
                child.key: 0,
            }

            for value in range(1, 4):
                await tx.send(value)
                assert await child.receive() == value

            state.recv_ready = (child.key, trio.Event())
            with warnings.catch_warnings():
                warnings.simplefilter('error', DeprecationWarning)
                stats = state.statistics()

            assert stats['queued_len_by_task'] == {
                brx.key: 3,
                child.key: 0,
            }
            assert stats['tasks_waiting'] == 0
            state.recv_ready = None

    trio.run(main)


def test_underlying_receive_failure_wakes_all_subscribers() -> None:
    '''
    A shared receive failure must terminate every broadcast receiver.

    Previously, only `EndOfChannel` and receiver cancellation woke
    peer tasks waiting on `BroadcastState.recv_ready`. If the shared
    underlying receiver raised another error, its owner propagated
    the failure and cleared the event while every peer remained
    blocked forever.

    Script one successful receive followed by a controlled
    `RuntimeError`. Let a fast child own both underlying receives
    while the root first drains its retained value and then waits on
    the child's second receive. Release the failure only after both
    tasks have reached those positions. Both exact errors prove the
    peer was awakened without losing buffered data. A later
    subscriber proves the terminal failure remains published for new
    receivers instead of retrying the failed underlying channel.

    '''
    class FailingReceiver:
        '''
        Return one value, then fail after deterministic release.

        '''
        def __init__(self) -> None:
            self.calls: int = 0
            self.failure_started = trio.Event()
            self.release_failure = trio.Event()

        async def receive(self) -> int:
            '''
            Drive the scripted success-then-failure sequence.

            '''
            self.calls += 1
            if self.calls == 1:
                return 1

            self.failure_started.set()
            await self.release_failure.wait()
            raise RuntimeError('underlying receive failed')

    async def main() -> None:
        source = FailingReceiver()
        brx = broadcast_receiver(source, 3)
        child_error: list[RuntimeError] = []
        root_error: list[BroadcastReceiveError] = []
        late_error: list[BroadcastReceiveError] = []
        root_drained = trio.Event()

        async def receive_child() -> None:
            async with brx.subscribe() as child:
                assert await child.receive() == 1
                try:
                    await child.receive()
                except RuntimeError as exc:
                    child_error.append(exc)

        async def receive_root() -> None:
            assert await brx.receive() == 1
            root_drained.set()
            try:
                await brx.receive()
            except BroadcastReceiveError as exc:
                root_error.append(exc)

        with trio.fail_after(1):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(receive_child)
                await source.failure_started.wait()

                nursery.start_soon(receive_root)
                await root_drained.wait()

                source.release_failure.set()

        assert source.calls == 2
        assert [str(exc) for exc in child_error] == [
            'underlying receive failed',
        ]
        assert [str(exc) for exc in root_error] == [
            'Shared broadcast receiver failed',
        ]
        assert child_error[0] is not root_error[0]
        assert root_error[0].__cause__ is child_error[0]

        async with brx.subscribe() as late:
            with pytest.raises(
                BroadcastReceiveError,
                match='Shared broadcast receiver failed',
            ) as exc_info:
                await late.receive()
            late_error.append(exc_info.value)
        assert late_error[0] is not child_error[0]
        assert late_error[0] is not root_error[0]
        assert late_error[0].__cause__ is child_error[0]
        assert source.calls == 2

    trio.run(main)


def test_control_flow_exit_wakes_broadcast_peer() -> None:
    '''
    Non-terminal control flow must wake peers without being retained.

    Process-control and cancellation-like `BaseException` values
    should remain local to the task which receives them, but the old
    owner still has to wake subscribers blocked on its shared event.
    Make one child own a controlled `BaseException` receive while the
    root waits behind it. After release, prove the child gets that
    exact exit and the root takes ownership of the next underlying
    receive instead of hanging or replaying the control-flow event.

    '''
    class ReceiveExit(BaseException):
        '''
        Model a non-terminal process-control receive exit.

        '''

    class ControlFlowReceiver:
        '''
        Raise one controlled exit, then return a value.

        '''
        def __init__(self) -> None:
            self.calls: int = 0
            self.exit_started = trio.Event()
            self.release_exit = trio.Event()

        async def receive(self) -> int:
            '''
            Drive the scripted control-flow-then-value sequence.

            '''
            self.calls += 1
            if self.calls == 1:
                self.exit_started.set()
                await self.release_exit.wait()
                raise ReceiveExit

            return 2

    async def main() -> None:
        source = ControlFlowReceiver()
        brx = broadcast_receiver(source, 3)
        child_exit: list[ReceiveExit] = []
        root_value: list[int] = []

        async def receive_child() -> None:
            async with brx.subscribe() as child:
                try:
                    await child.receive()
                except ReceiveExit as exc:
                    child_exit.append(exc)

        async def receive_root() -> None:
            root_value.append(await brx.receive())

        with trio.fail_after(1):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(receive_child)
                await source.exit_started.wait()
                nursery.start_soon(receive_root)

                while True:
                    _, event = brx._state.recv_ready
                    if event.statistics().tasks_waiting:
                        break
                    await trio.lowlevel.checkpoint()

                source.release_exit.set()

        assert len(child_exit) == 1
        assert root_value == [2]
        assert source.calls == 2
        assert brx._state.receive_exc is None

    trio.run(main)


def test_closing_non_owner_preserves_source_wait() -> None:
    '''
    Closing one subscriber must not wake another receiver's peers.

    `BroadcastReceiver.aclose()` previously set the one shared
    `BroadcastState.recv_ready` event even when a different receiver
    owned the source read. Waiting peers then repeatedly awaited an
    already-set event until the source produced another value,
    creating a runnable hot loop on idle streams.

    Block one child in the source receive, then place both the root
    and a closing child behind its event. Close only that waiting
    child and prove it gets `ClosedResourceError` without setting the
    shared event. Both remaining receivers must still get the same
    value after the source is released.

    '''
    async def main() -> None:
        tx, rx = trio.open_memory_channel(1)
        brx = broadcast_receiver(rx, 3)
        owner_value: list[int] = []
        root_value: list[int] = []
        closing_closed = trio.Event()

        async with (
            brx.subscribe() as owner,
            brx.subscribe() as closing,
        ):
            async def receive_owner() -> None:
                owner_value.append(await owner.receive())

            async def receive_root() -> None:
                root_value.append(await brx.receive())

            async def receive_closing() -> None:
                with pytest.raises(trio.ClosedResourceError):
                    await closing.receive()
                closing_closed.set()

            with trio.fail_after(1):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(receive_owner)
                    while brx._state.recv_ready is None:
                        await trio.lowlevel.checkpoint()

                    nursery.start_soon(receive_root)
                    nursery.start_soon(receive_closing)
                    _, event = brx._state.recv_ready
                    while event.statistics().tasks_waiting < 2:
                        await trio.lowlevel.checkpoint()

                    await closing.aclose()
                    await closing_closed.wait()
                    assert not event.is_set()
                    await tx.send(1)

        assert owner_value == [1]
        assert root_value == [1]

    trio.run(main)


@pytest.mark.parametrize(
    'first_outcome',
    [
        1,
        RuntimeError('discarded source error'),
        trio.EndOfChannel(),
    ],
)
def test_closing_source_owner_hands_read_to_peer(
    first_outcome: int|Exception,
) -> None:
    '''
    Closing the source-read owner must transfer ownership to a peer.

    Merely suppressing the old shared-event wake would leave peers
    blocked behind an externally closed receiver that still owned an
    idle source read. Script a first receive which blocks until its
    private scope is cancelled and a second which returns immediately.
    Close that owner only after the root is waiting behind it. Cover
    a shielded value, ordinary error and EOC from the cancelled source
    read. The owner must always get `ClosedResourceError`, while the
    awakened root takes the second source read without publishing the
    discarded source outcome.

    '''
    class HandoffReceiver:
        '''
        Block the first source read and satisfy the second.

        '''
        def __init__(self) -> None:
            self.calls: int = 0
            self.first_started = trio.Event()
            self.release_first = trio.Event()

        async def receive(self) -> int:
            '''
            Drive one cancelled read followed by one value.

            '''
            self.calls += 1
            if self.calls == 1:
                self.first_started.set()
                with trio.CancelScope(shield=True):
                    await self.release_first.wait()
                if isinstance(first_outcome, BaseException):
                    raise first_outcome
                return first_outcome

            return 2

    async def main() -> None:
        source = HandoffReceiver()
        brx = broadcast_receiver(source, 3)
        owner_closed = trio.Event()
        root_value: list[int] = []

        async with brx.subscribe() as owner:
            async def receive_owner() -> None:
                with pytest.raises(trio.ClosedResourceError):
                    await owner.receive()
                owner_closed.set()

            async def receive_root() -> None:
                root_value.append(await brx.receive())

            with trio.fail_after(1):
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(receive_owner)
                    await source.first_started.wait()
                    nursery.start_soon(receive_root)

                    _, event = brx._state.recv_ready
                    while not event.statistics().tasks_waiting:
                        await trio.lowlevel.checkpoint()

                    await owner.aclose()
                    source.release_first.set()
                    await owner_closed.wait()

        assert source.calls == 2
        assert root_value == [2]
        assert brx._state.receive_exc is None
        assert not brx._state.eoc

    trio.run(main)


def test_ensure_slow_consumers_lag_out(
    reg_addr,
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

            async with trio.open_nursery() as tn:

                for i in range(1, num_laggers):

                    task_name = f'sub_{i}'
                    laggers[task_name] = 0
                    tn.start_soon(
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

                # all no_overruns entries in the underlying
                # channel should have been copied into the bcaster
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
                assert cs.cancelled_caught

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


def test_no_raise_on_lag():
    '''
    Run a simple 2-task broadcast where one task is slow but configured
    so that it does not raise `Lagged` on overruns using
    `raise_on_lasg=False` and verify that the task does not raise.

    '''
    size = 100
    tx, rx = trio.open_memory_channel(size)
    brx = broadcast_receiver(rx, size)

    async def slow():
        async with brx.subscribe(
            raise_on_lag=False,
        ) as br:
            async for msg in br:
                print(f'slow task got: {msg}')
                await trio.sleep(0.1)

    async def fast():
        async with brx.subscribe() as br:
            async for msg in br:
                print(f'fast task got: {msg}')

    async def main():
        async with (
            tractor.open_root_actor(
                # NOTE: so we see the warning msg emitted by the bcaster
                # internals when the no raise flag is set.
                loglevel='warning',
            ),
            collapse_eg(),
            trio.open_nursery() as n,
        ):
            n.start_soon(slow)
            n.start_soon(fast)

            for i in range(1000):
                await tx.send(i)

            # simulate user nailing ctl-c after realizing
            # there's a lag in the slow task.
            await trio.sleep(1)
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        trio.run(main)


@pytest.mark.parametrize(
    ('subscribe', 'chan_attr'),
    [
        (tractor.MsgStream.subscribe, '_rx_chan'),
        (LinkedTaskChannel.subscribe, '_from_aio'),
    ],
    ids=['msg-stream', 'linked-task-channel'],
)
def test_stream_subscribe_forwards_lag_policy(
    subscribe,
    chan_attr: str,
) -> None:
    '''
    Stream wrappers must expose per-subscriber lag policy.

    `MsgStream.subscribe()` and `LinkedTaskChannel.subscribe()`
    previously omitted `BroadcastReceiver.raise_on_lag`, forcing
    downstream users to mutate a private receiver attribute. Invoke
    each public wrapper against a minimal receive-compatible handle.
    Prove the first non-raising subscription configures both the
    irreversible root broadcaster and its child, while a later strict
    child selects its own policy without changing that root.

    '''
    class StreamHandle:
        '''
        Provide the wrapper fields needed for local fan-out.

        '''
        def __init__(self) -> None:
            self._broadcaster = None
            setattr(
                self,
                chan_attr,
                SimpleNamespace(
                    _state=SimpleNamespace(max_buffer_size=1),
                ),
            )

        async def receive(self):
            '''
            Block if a regression unexpectedly enters source receive.

            '''
            await trio.sleep_forever()

        async def send(self, value) -> None:
            '''
            Satisfy `MsgStream` duplex-handle patching.

            '''

    async def main() -> None:
        stream = StreamHandle()
        async with subscribe(
            stream,
            raise_on_lag=False,
        ) as first:
            assert not stream._broadcaster._raise_on_lag
            assert not first._raise_on_lag

        async with subscribe(
            stream,
            raise_on_lag=True,
        ) as second:
            assert not stream._broadcaster._raise_on_lag
            assert second._raise_on_lag

    trio.run(main)
