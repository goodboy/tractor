# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
``tokio`` style broadcast channel.
https://docs.rs/tokio/1.11.0/tokio/sync/broadcast/index.html

'''
from __future__ import annotations
from abc import abstractmethod
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from operator import ne
from typing import Optional, Callable, Awaitable, Any, AsyncIterator, Protocol
from typing import Generic, TypeVar

import trio
from trio._core._run import Task
from trio.abc import ReceiveChannel
from trio.lowlevel import current_task


# A regular invariant generic type
T = TypeVar("T")

# covariant because AsyncReceiver[Derived] can be passed to someone
# expecting AsyncReceiver[Base])
ReceiveType = TypeVar("ReceiveType", covariant=True)


class AsyncReceiver(
    Protocol,
    Generic[ReceiveType],
):
    '''
    An async receivable duck-type that quacks much like trio's
    ``trio.abc.ReceiveChannel``.

    '''
    @abstractmethod
    async def receive(self) -> ReceiveType:
        ...

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
    async def __aenter__(self) -> AsyncReceiver[ReceiveType]:
        ...

    @abstractmethod
    async def __aexit__(self, *args) -> None:
        ...


class Lagged(trio.TooSlowError):
    '''
    Subscribed consumer task was too slow and was overrun
    by the fastest consumer-producer pair.

    '''


@dataclass
class BroadcastState:
    '''
    Common state to all receivers of a broadcast.

    '''
    queue: deque
    maxlen: int

    # map of underlying instance id keys to receiver instances which
    # must be provided as a singleton per broadcaster set.
    subs: dict[int, int]

    # broadcast event to wake up all sleeping consumer tasks
    # on a newly produced value from the sender.
    recv_ready: Optional[tuple[int, trio.Event]] = None

    # if a ``trio.EndOfChannel`` is received on any
    # consumer all consumers should be placed in this state
    # such that the group is notified of the end-of-broadcast.
    # For now, this is solely for testing/debugging purposes.
    eoc: bool = False

    # If the broadcaster was cancelled, we might as well track it
    cancelled: bool = False


class BroadcastReceiver(ReceiveChannel):
    '''
    A memory receive channel broadcaster which is non-lossy for the
    fastest consumer.

    Additional consumer tasks can receive all produced values by registering
    with ``.subscribe()`` and receiving from the new instance it delivers.

    '''
    def __init__(
        self,

        rx_chan: AsyncReceiver,
        state: BroadcastState,
        receive_afunc: Optional[Callable[[], Awaitable[Any]]] = None,

    ) -> None:

        # register the original underlying (clone)
        self.key = id(self)
        self._state = state
        state.subs[self.key] = -1

        # underlying for this receiver
        self._rx = rx_chan
        self._recv = receive_afunc or rx_chan.receive
        self._closed: bool = False

    async def receive(self) -> ReceiveType:

        key = self.key
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
            if self._closed:
                raise trio.ClosedResourceError

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
                # https://docs.rs/tokio/1.11.0/tokio/sync/broadcast/index.html#lagging

                # decrement to the last value and expect
                # consumer to either handle the ``Lagged`` and come back
                # or bail out on its own (thus un-subscribing)
                state.subs[key] = state.maxlen - 1

                # this task was overrun by the producer side
                task: Task = current_task()
                raise Lagged(f'Task {task.name} was overrun')

            state.subs[key] -= 1
            return value

        # current task already has the latest value **and** is the
        # first task to begin waiting for a new one
        if state.recv_ready is None:

            if self._closed:
                raise trio.ClosedResourceError

            event = trio.Event()
            state.recv_ready = key, event

            # if we're cancelled here it should be
            # fine to bail without affecting any other consumers
            # right?
            try:
                value = await self._recv()

                # items with lower indices are "newer"
                # NOTE: ``collections.deque`` implicitly takes care of
                # trucating values outside our ``state.maxlen``. In the
                # alt-backend-array-case we'll need to make sure this is
                # implemented in similar ringer-buffer-ish style.
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

                # NOTE: this should ONLY be set if the above task was *NOT*
                # cancelled on the `._recv()` call.
                event.set()
                return value

            except trio.EndOfChannel:
                # if any one consumer gets an EOC from the underlying
                # receiver we need to unblock and send that signal to
                # all other consumers.
                self._state.eoc = True
                if event.statistics().tasks_waiting:
                    event.set()
                raise

            except (
                trio.Cancelled,
            ):
                # handle cancelled specially otherwise sibling
                # consumers will be awoken with a sequence of -1
                # and will potentially try to rewait the underlying
                # receiver instead of just cancelling immediately.
                self._state.cancelled = True
                if event.statistics().tasks_waiting:
                    event.set()
                raise

            finally:

                # Reset receiver waiter task event for next blocking condition.
                # this MUST be reset even if the above ``.recv()`` call
                # was cancelled to avoid the next consumer from blocking on
                # an event that won't be set!
                state.recv_ready = None

        # This task is all caught up and ready to receive the latest
        # value, so queue sched it on the internal event.
        else:
            seq = state.subs[key]
            assert seq == -1  # sanity
            _, ev = state.recv_ready
            await ev.wait()

            # NOTE: if we ever would like the behaviour where if the
            # first task to recv on the underlying is cancelled but it
            # still DOES trigger the ``.recv_ready``, event we'll likely need
            # this logic:

            if seq > -1:
                # stuff from above..
                seq = state.subs[key]

                value = state.queue[seq]
                state.subs[key] -= 1
                return value

            elif seq == -1:
                # XXX: In the case where the first task to allocate the
                # ``.recv_ready`` event is cancelled we will be woken with
                # a non-incremented sequence number and thus will read the
                # oldest value if we use that. Instead we need to detect if
                # we have not been incremented and then receive again.
                return await self.receive()

            else:
                raise ValueError(f'Invalid sequence {seq}!?')

    @asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncIterator[BroadcastReceiver]:
        '''
        Subscribe for values from this broadcast receiver.

        Returns a new ``BroadCastReceiver`` which is registered for and
        pulls data from a clone of the original
        ``trio.abc.ReceiveChannel`` provided at creation.

        '''
        if self._closed:
            raise trio.ClosedResourceError

        state = self._state
        br = BroadcastReceiver(
            rx_chan=self._rx,
            state=state,
            receive_afunc=self._recv,
        )
        # assert clone in state.subs
        assert br.key in state.subs

        try:
            yield br
        finally:
            await br.aclose()

    async def aclose(
        self,
    ) -> None:
        '''
        Close this receiver without affecting other consumers.

        '''
        if self._closed:
            return

        # if there are sleeping consumers wake
        # them on closure.
        rr = self._state.recv_ready
        if rr:
            _, event = rr
            event.set()

        # XXX: leaving it like this consumers can still get values
        # up to the last received that still reside in the queue.
        self._state.subs.pop(self.key)
        self._closed = True


def broadcast_receiver(

    recv_chan: AsyncReceiver,
    max_buffer_size: int,
    **kwargs,

) -> BroadcastReceiver:

    return BroadcastReceiver(
        recv_chan,
        state=BroadcastState(
            queue=deque(maxlen=max_buffer_size),
            maxlen=max_buffer_size,
            subs={},
        ),
        **kwargs,
    )
