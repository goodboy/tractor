'''
``tokio`` style broadcast channel.
https://tokio-rs.github.io/tokio/doc/tokio/sync/broadcast/index.html

'''
from __future__ import annotations
from itertools import cycle
from collections import deque
from contextlib import asynccontextmanager
from functools import partial
from typing import Optional

import trio
import tractor
from trio.lowlevel import current_task
from trio.abc import ReceiveChannel
from trio._core._run import Task


class Lagged(trio.TooSlowError):
    '''Subscribed consumer task was too slow'''


class BroadcastReceiver(ReceiveChannel):
    '''A memory receive channel broadcaster which is non-lossy for the
    fastest consumer.

    Additional consumer tasks can receive all produced values by registering
    with ``.subscribe()`` and receiving from thew new instance it delivers.

    '''
    # map of underlying clones to receiver wrappers
    _subs: dict[trio.ReceiveChannel, BroadcastReceiver] = {}

    def __init__(
        self,

        rx_chan: ReceiveChannel,
        queue: deque,

    ) -> None:

        self._rx = rx_chan
        self._queue = queue
        self._value_received: Optional[trio.Event] = None

    async def receive(self):

        key = self._rx

        # TODO: ideally we can make some way to "lock out" the
        # underlying receive channel in some way such that if some task
        # tries to pull from it directly (i.e. one we're unaware of)
        # then it errors out.

        # only tasks which have entered ``.subscribe()`` can
        # receive on this broadcaster.
        try:
            seq = self._subs[key]
        except KeyError:
            raise RuntimeError(
                f'{self} is not registerd as subscriber')

        # check that task does not already have a value it can receive
        # immediately and/or that it has lagged.
        if seq > -1:
            # get the oldest value we haven't received immediately
            try:
                value = self._queue[seq]
            except IndexError:

                # adhere to ``tokio`` style "lagging":
                # "Once RecvError::Lagged is returned, the lagging
                # receiver's position is updated to the oldest value
                # contained by the channel. The next call to recv will
                # return this value."
                # https://tokio-rs.github.io/tokio/doc/tokio/sync/broadcast/index.html#lagging

                # decrement to the last value and expect
                # consumer to either handle the ``Lagged`` and come back
                # or bail out on its own (thus un-subscribing)
                self._subs[key] = self._queue.maxlen - 1

                # this task was overrun by the producer side
                task: Task = current_task()
                raise Lagged(f'Task {task.name} was overrun')

            self._subs[key] -= 1
            return value

        # current task already has the latest value **and** is the
        # first task to begin waiting for a new one
        if self._value_received is None:

            event = self._value_received = trio.Event()
            value = await self._rx.receive()

            # items with lower indices are "newer"
            self._queue.appendleft(value)

            # broadcast new value to all subscribers by increasing
            # all sequence numbers that will point in the queue to
            # their latest available value.

            subs = self._subs.copy()
            # don't decrement the sequence # for this task since we
            # already retreived the last value
            subs.pop(key)
            for sub_key, seq in subs.items():
                self._subs[sub_key] += 1

            # reset receiver waiter task event for next blocking condition
            self._value_received = None
            event.set()
            return value

        # This task is all caught up and ready to receive the latest
        # value, so queue sched it on the internal event.
        else:
            await self._value_received.wait()

            seq = self._subs[key]
            assert seq > -1, 'Internal error?'

            self._subs[key] -= 1
            return self._queue[0]

    @asynccontextmanager
    async def subscribe(
        self,
    ) -> BroadcastReceiver:
        '''Subscribe for values from this broadcast receiver.

        Returns a new ``BroadCastReceiver`` which is registered for and
        pulls data from a clone of the original ``trio.abc.ReceiveChannel``
        provided at creation.

        '''
        clone = self._rx.clone()
        self._subs[clone] = -1
        try:
            yield BroadcastReceiver(
                clone,
                self._queue,
            )
        finally:
            # drop from subscribers and close
            self._subs.pop(clone)
            # XXX: this is the reason this function is async: the
            # ``AsyncResource`` api.
            await clone.aclose()

    # TODO:
    # - should there be some ._closed flag that causes
    #   consumers to die **before** they read all queued values?
    # - if subs only open and close clones then the underlying
    #   will never be killed until the last instance closes?
    #   This is correct right?
    async def aclose(
        self,
    ) -> None:
        # XXX: leaving it like this consumers can still get values
        # up to the last received that still reside in the queue.
        # Is this what we want?
        await self._rx.aclose()
        self._subs.pop(self._rx)


def broadcast_receiver(

    recv_chan: ReceiveChannel,
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

            retries = 3
            size = 100
            tx, rx = trio.open_memory_channel(size)
            rx = broadcast_receiver(rx, size)

            async def sub_and_print(
                delay: float,
            ) -> None:

                task = current_task()
                count = 0

                while True:
                    async with rx.subscribe() as brx:
                        try:
                            async for value in brx:
                                print(f'{task.name}: {value}')
                                await trio.sleep(delay)
                                count += 1

                        except Lagged:
                            print(
                                f'restarting slow ass {task.name}'
                                f'that bailed out on {count}:{value}')
                            if count <= retries:
                                continue
                            else:
                                print(
                                    f'{task.name} was too slow and terminated '
                                    f'on {count}:{value}')
                                return

            async with trio.open_nursery() as n:
                for i in range(1, size):
                    n.start_soon(
                        partial(
                            sub_and_print,
                            delay=i*0.01,
                        ),
                        name=f'sub_{i}',
                    )

                async with tx:
                    for i in cycle(range(size)):
                        print(f'sending: {i}')
                        await tx.send(i)

    trio.run(main)
