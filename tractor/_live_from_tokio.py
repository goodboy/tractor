'''
``tokio`` style broadcast channel.
https://tokio-rs.github.io/tokio/doc/tokio/sync/broadcast/index.html

'''
from __future__ import annotations
from itertools import cycle
from collections import deque
from contextlib import contextmanager
from functools import partial
from typing import Optional

import trio
import tractor
from trio.lowlevel import current_task
from trio.abc import ReceiveChannel
from trio._core._run import Task
from trio._channel import (
    MemoryReceiveChannel,
)


class Lagged(trio.TooSlowError):
    '''Subscribed consumer task was too slow'''


class BroadcastReceiver(ReceiveChannel):
    '''A memory receive channel broadcaster which is non-lossy for the
    fastest consumer.

    Additional consumer tasks can receive all produced values by registering
    with ``.subscribe()``.

    '''
    def __init__(
        self,

        rx_chan: MemoryReceiveChannel,
        queue: deque,

    ) -> None:

        self._rx = rx_chan
        self._queue = queue
        self._subs: dict[Task, int] = {}  # {id(current_task()): -1}
        self._clones: dict[Task, MemoryReceiveChannel] = {}
        self._value_received: Optional[trio.Event] = None

    async def receive(self):

        task: Task = current_task()

        # check that task does not already have a value it can receive
        # immediately and/or that it has lagged.
        try:
            seq = self._subs[task]
        except KeyError:
            raise RuntimeError(
                f'Task {task.name} is not registerd as subscriber')

        if seq > -1:
            # get the oldest value we haven't received immediately
            try:
                value = self._queue[seq]
            except IndexError:
                # decrement to the last value and expect
                # consumer to either handle the ``Lagged`` and come back
                # or bail out on it's own (thus un-subscribing)
                self._subs[task] = self._queue.maxlen - 1

                # this task was overrun by the producer side
                raise Lagged(f'Task {task.name} was overrun')

            self._subs[task] -= 1
            return value

        if self._value_received is None:
            # current task already has the latest value **and** is the
            # first task to begin waiting for a new one

            # what sanity checks might we use for the underlying chan ?
            # assert not self._rx._state.data

            event = self._value_received = trio.Event()
            value = await self._rx.receive()

            # items with lower indices are "newer"
            self._queue.appendleft(value)

            # broadcast new value to all subscribers by increasing
            # all sequence numbers that will point in the queue to
            # their latest available value.

            subs = self._subs.copy()
            # don't decerement the sequence # for this task since we
            # already retreived the last value
            subs.pop(task)
            for sub_key, seq in subs.items():
                self._subs[sub_key] += 1

            # reset receiver waiter task event for next blocking condition
            self._value_received = None
            event.set()
            return value

        else:
            await self._value_received.wait()

            seq = self._subs[task]
            assert seq > -1, 'Internal error?'

            self._subs[task] -= 1
            return self._queue[0]

    # @asynccontextmanager
    @contextmanager
    def subscribe(
        self,
    ) -> BroadcastReceiver:
        task: task = current_task()
        self._subs[task] = -1
        # XXX: we only use this clone for closure tracking
        clone = self._clones[task] = self._rx.clone()
        try:
            yield self
        finally:
            self._subs.pop(task)
            clone.close()

    # TODO: do we need anything here?
    # if we're the last sub to close then close
    # the underlying rx channel, but couldn't we just
    # use ``.clone()``s trackign then?
    async def aclose(self) -> None:
        task: Task = current_task()
        await self._clones[task].aclose()


def broadcast_receiver(

    recv_chan: MemoryReceiveChannel,
    max_buffer_size: int,

) -> BroadcastReceiver:

    return BroadcastReceiver(
        recv_chan,
        queue=deque(maxlen=max_buffer_size),
    )


if __name__ == '__main__':

    async def main():

        async with tractor.open_root_actor(
            debug_mode=True,
            # loglevel='info',
        ):

            size = 100
            tx, rx = trio.open_memory_channel(size)
            rx = broadcast_receiver(rx, size)

            async def sub_and_print(
                delay: float,
            ) -> None:

                task = current_task()
                count = 0

                while True:
                    with rx.subscribe():
                        try:
                            async for value in rx:
                                print(f'{task.name}: {value}')
                                await trio.sleep(delay)
                                count += 1

                        except Lagged:
                            print(
                                f'restarting slow ass {task.name}'
                                f'that bailed out on {count}:{value}')
                            continue

            async with trio.open_nursery() as n:
                for i in range(1, 10):
                    n.start_soon(
                        partial(
                            sub_and_print,
                            delay=i*0.01,
                        ),
                        name=f'sub_{i}',
                    )

                async with tx:
                    for i in cycle(range(1000)):
                        print(f'sending: {i}')
                        await tx.send(i)

    trio.run(main)
