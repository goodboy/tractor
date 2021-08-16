'''
``tokio`` style broadcast channel.
https://tokio-rs.github.io/tokio/doc/tokio/sync/broadcast/index.html

'''
from __future__ import annotations
from abc import abstractmethod
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from itertools import cycle
from operator import ne
from typing import Optional, Callable, Awaitable, Any, AsyncIterator, Protocol
from typing import Generic, TypeVar

import trio
from trio._core._run import Task
from trio.abc import ReceiveChannel
from trio.lowlevel import current_task
import tractor


# A regular invariant generic type
T = TypeVar("T")

# The type of object produced by a ReceiveChannel (covariant because
# ReceiveChannel[Derived] can be passed to someone expecting
# ReceiveChannel[Base])
ReceiveType = TypeVar("ReceiveType", covariant=True)


class CloneableReceiveChannel(
    Protocol,
    Generic[ReceiveType],
):
    @abstractmethod
    def clone(self) -> CloneableReceiveChannel[ReceiveType]:
        '''Clone this receiver usually by making a copy.'''

    @abstractmethod
    async def receive(self) -> ReceiveType:
        '''Same as in ``trio``.'''

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[ReceiveType]:
        ...

    @abstractmethod
    async def __anext__(self) -> ReceiveType:
        ...

    # ``trio.abc.AsyncResource`` methods
    @abstractmethod
    async def aclose(self):
        ...

    @abstractmethod
    async def __aenter__(self) -> CloneableReceiveChannel[ReceiveType]:
        ...

    @abstractmethod
    async def __aexit__(self, *args) -> None:
        ...


class Lagged(trio.TooSlowError):
    '''Subscribed consumer task was too slow'''


@dataclass
class BroadcastState:
    '''Common state to all receivers of a broadcast.

    '''
    queue: deque

    # map of underlying clones to receiver wrappers
    # which must be provided as a singleton per broadcaster
    # clone-subscription set.
    subs: dict[CloneableReceiveChannel, int]

    # broadcast event to wakeup all sleeping consumer tasks
    # on a newly produced value from the sender.
    sender_ready: Optional[trio.Event] = None


class BroadcastReceiver(ReceiveChannel):
    '''A memory receive channel broadcaster which is non-lossy for the
    fastest consumer.

    Additional consumer tasks can receive all produced values by registering
    with ``.subscribe()`` and receiving from thew new instance it delivers.

    '''
    def __init__(
        self,

        rx_chan: CloneableReceiveChannel,
        state: BroadcastState,
        receive_afunc: Optional[Callable[[], Awaitable[Any]]] = None,

    ) -> None:

        # register the original underlying (clone)
        self._state = state
        state.subs[rx_chan] = -1

        # underlying for this receiver
        self._rx = rx_chan
        self._recv = receive_afunc or rx_chan.receive

    async def receive(self):

        key = self._rx
        state = self._state

        # TODO: ideally we can make some way to "lock out" the
        # underlying receive channel in some way such that if some task
        # tries to pull from it directly (i.e. one we're unaware of)
        # then it errors out.

        # only tasks which have entered ``.subscribe()`` can
        # receive on this broadcaster.
        try:
            seq = state.subs[key]
        except KeyError:
            raise RuntimeError(
                f'{self} is not registerd as subscriber')

        # check that task does not already have a value it can receive
        # immediately and/or that it has lagged.
        if seq > -1:
            # get the oldest value we haven't received immediately
            try:
                value = state.queue[seq]
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
                state.subs[key] = state.queue.maxlen - 1

                # this task was overrun by the producer side
                task: Task = current_task()
                raise Lagged(f'Task {task.name} was overrun')

            state.subs[key] -= 1
            return value

        # current task already has the latest value **and** is the
        # first task to begin waiting for a new one
        if state.sender_ready is None:

            event = state.sender_ready = trio.Event()
            value = await self._recv()

            # items with lower indices are "newer"
            state.queue.appendleft(value)

            # broadcast new value to all subscribers by increasing
            # all sequence numbers that will point in the queue to
            # their latest available value.

            # don't decrement the sequence for this task since we
            # already retreived the last value

            # XXX: which of these impls is fastest?

            # subs = state.subs.copy()
            # subs.pop(key)

            for sub_key in filter(
                # lambda k: k != key, state.subs,
                partial(ne, key), state.subs,
            ):
                state.subs[sub_key] += 1

            # reset receiver waiter task event for next blocking condition
            event.set()
            state.sender_ready = None
            return value

        # This task is all caught up and ready to receive the latest
        # value, so queue sched it on the internal event.
        else:
            await state.sender_ready.wait()
            seq = state.subs[key]
            state.subs[key] -= 1
            return state.queue[seq]

    @asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncIterator[BroadcastReceiver]:
        '''Subscribe for values from this broadcast receiver.

        Returns a new ``BroadCastReceiver`` which is registered for and
        pulls data from a clone of the original ``trio.abc.ReceiveChannel``
        provided at creation.

        '''
        # if we didn't want to enforce "clone-ability" how would
        # we key arbitrary subscriptions? Use a token system?
        clone = self._rx.clone()
        state = self._state
        br = BroadcastReceiver(
            rx_chan=clone,
            state=state,
        )
        assert clone in state.subs

        try:
            yield br
        finally:
            # XXX: this is the reason this function is async: the
            # ``AsyncResource`` api.
            await clone.aclose()
            # drop from subscribers and close
            state.subs.pop(clone)

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
        self._state.subs.pop(self._rx)


def broadcast_receiver(

    recv_chan: CloneableReceiveChannel,
    max_buffer_size: int,
    **kwargs,

) -> BroadcastReceiver:

    return BroadcastReceiver(
        recv_chan,
        state=BroadcastState(
            queue=deque(maxlen=max_buffer_size),
            subs={},
        ),
        **kwargs,
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
                lags = 0

                while True:
                    async with rx.subscribe() as brx:
                        try:
                            async for value in brx:
                                print(f'{task.name}: {value}')
                                await trio.sleep(delay)

                        except Lagged:
                            print(
                                f'restarting slow ass {task.name}'
                                f'that bailed out on {lags}:{value}')
                            if lags <= retries:
                                lags += 1
                                continue
                            else:
                                print(
                                    f'{task.name} was too slow and terminated '
                                    f'on {lags}:{value}')
                                return

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
                    for i in cycle(range(size)):
                        print(f'sending: {i}')
                        await tx.send(i)

    trio.run(main)
