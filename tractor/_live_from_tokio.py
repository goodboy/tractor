'''
``tokio`` style broadcast channels.

'''
from __future__ import annotations
# from math import inf
from itertools import cycle
from collections import deque
from contextlib import contextmanager  # , asynccontextmanager
from functools import partial
from typing import Optional

import trio
import tractor
from trio.lowlevel import current_task
from trio.abc import ReceiveChannel  # , SendChannel
# from trio._core import enable_ki_protection
from trio._core._run import Task
from trio._channel import (
    MemorySendChannel,
    MemoryReceiveChannel,
    # MemoryChannelState,
)


class Lagged(trio.TooSlowError):
    '''Subscribed consumer task was too slow'''


class BroadcastReceiver(ReceiveChannel):
    '''This isn't Paris, not Berlin, nor Honk Kong..

    '''
    def __init__(
        self,
        rx_chan: MemoryReceiveChannel,
        buffer_size: int = 100,

    ) -> None:

        self._rx = rx_chan
        self._len = buffer_size
        self._queue = deque(maxlen=buffer_size)
        self._subs = {id(current_task()): -1}
        self._value_received: Optional[trio.Event] = None

    async def receive(self):

        task: Task
        task = current_task()

        # check that task does not already have a value it can receive
        # immediately and/or that it has lagged.
        key = id(task)
        # print(key)
        try:
            seq = self._subs[key]
        except KeyError:
            self._subs.pop(key)
            raise RuntimeError(
                f'Task {task.name} is not registerd as subscriber')

        if seq > -1:
            # get the oldest value we haven't received immediately

            try:
                value = self._queue[seq]
            except IndexError:
                raise Lagged(f'Task {task.name} was overrun')

            self._subs[key] -= 1
            return value

        if self._value_received is None:
            # we already have the latest value **and** are the first
            # task to begin waiting for a new one

            # sanity checks with underlying chan ?
            # assert not self._rx._state.data

            event = self._value_received = trio.Event()
            value = await self._rx.receive()

            # items with lower indices are "newer"
            self._queue.appendleft(value)

            # broadcast new value to all subscribers by increasing
            # all sequence numbers that will point in the queue to
            # their latest available value.
            for sub_key, seq in self._subs.items():

                if key == sub_key:
                    # we don't need to increase **this** task's
                    # sequence number since we just consumed the latest
                    # value
                    continue

                # # except TypeError:
                # #     # already lagged
                # #     seq = Lagged

                self._subs[sub_key] += 1

            self._value_received = None
            event.set()
            return value

        else:
            await self._value_received.wait()

            seq = self._subs[key]
            assert seq > -1, 'Uhhhh'

            self._subs[key] -= 1
            return self._queue[0]

    # @asynccontextmanager
    @contextmanager
    def subscribe(
        self,

    ) -> BroadcastReceiver:
        key = id(current_task())
        self._subs[key] = -1
        try:
            yield self
        finally:
            self._subs.pop(key)

    async def aclose(self) -> None:
        # TODO: wtf should we do here?
        # if we're the last sub to close then close
        # the underlying rx channel
        pass


def broadcast_channel(

    max_buffer_size: int,

) -> (MemorySendChannel, BroadcastReceiver):

    tx, rx = trio.open_memory_channel(max_buffer_size)
    return tx, BroadcastReceiver(rx)


if __name__ == '__main__':

    async def main():

        async with tractor.open_root_actor(
            debug_mode=True,
            # loglevel='info',
        ):

            tx, rx = broadcast_channel(100)

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
