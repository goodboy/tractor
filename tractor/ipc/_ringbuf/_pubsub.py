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
Ring buffer ipc publish-subscribe mechanism brokered by ringd
can dynamically add new outputs (publisher) or inputs (subscriber)
'''
from typing import (
    TypeVar,
    Generic,
    Callable,
    Awaitable,
    AsyncContextManager
)
from functools import partial
from contextlib import asynccontextmanager as acm
from dataclasses import dataclass

import trio
import tractor

from tractor.ipc import (
    RingBufferSendChannel,
    RingBufferReceiveChannel,
    attach_to_ringbuf_sender,
    attach_to_ringbuf_receiver
)

from tractor.trionics import (
    order_send_channel,
    order_receive_channel
)
import tractor.ipc._ringbuf._ringd as ringd


log = tractor.log.get_logger(__name__)


ChannelType = TypeVar('ChannelType')


@dataclass
class ChannelInfo:
    name: str
    channel: ChannelType
    cancel_scope: trio.CancelScope


class ChannelManager(Generic[ChannelType]):
    '''
    Helper for managing channel resources and their handler tasks with
    cancellation, add or remove channels dynamically!

    '''

    def __init__(
        self,
        # nursery used to spawn channel handler tasks
        n: trio.Nursery,

        # acm will be used for setup & teardown of channel resources
        open_channel_acm: Callable[..., AsyncContextManager[ChannelType]],

        # long running bg task to handle channel
        channel_task: Callable[..., Awaitable[None]]
    ):
        self._n = n
        self._open_channel = open_channel_acm
        self._channel_task = channel_task

        # signal when a new channel conects and we previously had none
        self._connect_event = trio.Event()

        # store channel runtime variables
        self._channels: list[ChannelInfo] = []

        # methods that modify self._channels should be ordered by FIFO
        self._lock = trio.StrictFIFOLock()

        self._is_closed: bool = True

    @property
    def closed(self) -> bool:
        return self._is_closed

    @property
    def lock(self) -> trio.StrictFIFOLock:
        return self._lock

    @property
    def channels(self) -> list[ChannelInfo]:
        return self._channels

    async def _channel_handler_task(
        self,
        name: str,
        task_status: trio.TASK_STATUS_IGNORED,
        **kwargs
    ):
        '''
        Open channel resources, add to internal data structures, signal channel
        connect through trio.Event, and run `channel_task` with cancel scope,
        and finally, maybe remove channel from internal data structures.

        Spawned by `add_channel` function, lock is held from begining of fn
        until `task_status.started()` call.

        kwargs are proxied to `self._open_channel` acm.
        '''
        async with self._open_channel(name, **kwargs) as chan:
            cancel_scope = trio.CancelScope()
            info = ChannelInfo(
                name=name,
                channel=chan,
                cancel_scope=cancel_scope
            )
            self._channels.append(info)

            if len(self) == 1:
                self._connect_event.set()

            task_status.started()

            with cancel_scope:
                await self._channel_task(info)

        self._maybe_destroy_channel(name)

    def _find_channel(self, name: str) -> tuple[int, ChannelInfo] | None:
        '''
        Given a channel name maybe return its index and value from
        internal _channels list.

        Only use after acquiring lock.
        '''
        for entry in enumerate(self._channels):
            i, info = entry
            if info.name == name:
                return entry

        return None


    def _maybe_destroy_channel(self, name: str):
        '''
        If channel exists cancel its scope and remove from internal
        _channels list.

        '''
        maybe_entry = self._find_channel(name)
        if maybe_entry:
            i, info = maybe_entry
            info.cancel_scope.cancel()
            del self._channels[i]

    async def add_channel(self, name: str, **kwargs):
        '''
        Add a new channel to be handled

        '''
        if self.closed:
            raise trio.ClosedResourceError

        async with self._lock:
            await self._n.start(partial(
                self._channel_handler_task,
                name,
                **kwargs
            ))

    async def remove_channel(self, name: str):
        '''
        Remove a channel and stop its handling

        '''
        if self.closed:
            raise trio.ClosedResourceError

        async with self._lock:
            self._maybe_destroy_channel(name)

            # if that was last channel reset connect event
            if len(self) == 0:
                self._connect_event = trio.Event()

    async def wait_for_channel(self):
        '''
        Wait until at least one channel added

        '''
        if self.closed:
            raise trio.ClosedResourceError

        await self._connect_event.wait()
        self._connect_event = trio.Event()

    def __len__(self) -> int:
        return len(self._channels)

    def __getitem__(self, name: str):
        maybe_entry = self._find_channel(name)
        if maybe_entry:
            _, info = maybe_entry
            return info

        raise KeyError(f'Channel {name} not found!')

    def open(self):
        self._is_closed = False

    async def close(self) -> None:
        if self.closed:
            log.warning('tried to close ChannelManager but its already closed...')
            return

        for info in self._channels:
            await self.remove_channel(info.name)

        self._is_closed = True


'''
Ring buffer publisher & subscribe pattern mediated by `ringd` actor.

'''

@dataclass
class PublisherChannels:
    ring: RingBufferSendChannel
    schan: trio.MemorySendChannel
    rchan: trio.MemoryReceiveChannel


class RingBufferPublisher(trio.abc.SendChannel[bytes]):
    '''
    Use ChannelManager to create a multi ringbuf round robin sender that can
    dynamically add or remove more outputs.

    Don't instantiate directly, use `open_ringbuf_publisher` acm to manage its
    lifecycle.

    '''
    def __init__(
        self,
        n: trio.Nursery,

        # new ringbufs created will have this buf_size
        buf_size: int = 10 * 1024,

        # global batch size for all channels
        batch_size: int = 1
    ):
        self._buf_size = buf_size
        self._batch_size: int = batch_size

        self._chanmngr = ChannelManager[PublisherChannels](
            n,
            self._open_channel,
            self._channel_task
        )

        # methods that send data over the channels need to be acquire send lock
        # in order to guarantee order of operations
        self._send_lock = trio.StrictFIFOLock()

        self._next_turn: int = 0

        self._is_closed: bool = True

    @property
    def closed(self) -> bool:
        return self._is_closed

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @batch_size.setter
    def set_batch_size(self, value: int) -> None:
        for info in self.channels:
            info.channel.ring.batch_size = value

    @property
    def channels(self) -> list[ChannelInfo]:
        return self._chanmngr.channels

    def get_channel(self, name: str) -> ChannelInfo:
        '''
        Get underlying ChannelInfo from name

        '''
        return self._chanmngr[name]

    async def add_channel(
        self,
        name: str,
        must_exist: bool = False
    ):
        await self._chanmngr.add_channel(name, must_exist=must_exist)

    async def remove_channel(self, name: str):
        await self._chanmngr.remove_channel(name)

    @acm
    async def _open_channel(

        self,
        name: str,
        must_exist: bool = False

    ) -> AsyncContextManager[PublisherChannels]:
        '''
        Open a ringbuf through `ringd` and attach as send side
        '''
        async with (
            ringd.open_ringbuf(
                name=name,
                buf_size=self._buf_size,
                must_exist=must_exist,
            ) as token,
            attach_to_ringbuf_sender(token) as ring,
        ):
            schan, rchan = trio.open_memory_channel(0)
            yield PublisherChannels(
                ring=ring,
                schan=schan,
                rchan=rchan
            )
            try:
                while True:
                    msg = rchan.receive_nowait()
                    await ring.send(msg)

            except trio.WouldBlock:
                ...

    async def _channel_task(self, info: ChannelInfo) -> None:
        '''
        Forever get current runtime info for channel, wait on its next pending
        payloads update event then drain all into send channel.

        '''
        try:
            async for msg in info.channel.rchan:
                await info.channel.ring.send(msg)

        except trio.Cancelled:
            ...

    async def send(self, msg: bytes):
        '''
        If no output channels connected, wait until one, then fetch the next
        channel based on turn, add the indexed payload and update
        `self._next_turn` & `self._next_index`.

        Needs to acquire `self._send_lock` to make sure updates to turn & index
        variables dont happen out of order.

        '''
        if self.closed:
            raise trio.ClosedResourceError

        if self._send_lock.locked():
            raise trio.BusyResourceError

        async with self._send_lock:
            # wait at least one decoder connected
            if len(self.channels) == 0:
                await self._chanmngr.wait_for_channel()

            if self._next_turn >= len(self.channels):
                self._next_turn = 0

            info = self.channels[self._next_turn]
            await info.channel.schan.send(msg)

            self._next_turn += 1

    async def flush(self, new_batch_size: int | None = None):
        async with self._chanmngr.lock:
            for info in self.channels:
                await info.channel.ring.flush(new_batch_size=new_batch_size)

    async def __aenter__(self):
        self._chanmngr.open()
        self._is_closed = False
        return self

    async def aclose(self) -> None:
        if self.closed:
            log.warning('tried to close RingBufferPublisher but its already closed...')
            return

        await self._chanmngr.close()
        self._is_closed = True


@acm
async def open_ringbuf_publisher(

    buf_size: int = 10 * 1024,
    batch_size: int = 1,
    guarantee_order: bool = False,
    force_cancel: bool = False

) -> AsyncContextManager[RingBufferPublisher]:
    '''
    Open a new ringbuf publisher

    '''
    async with (
        trio.open_nursery() as n,
        RingBufferPublisher(
            n,
            buf_size=buf_size,
            batch_size=batch_size
        ) as publisher
    ):
        if guarantee_order:
            order_send_channel(publisher)

        yield publisher

        if force_cancel:
            # implicitly cancel any running channel handler task
            n.cancel_scope.cancel()


class RingBufferSubscriber(trio.abc.ReceiveChannel[bytes]):
    '''
    Use ChannelManager to create a multi ringbuf receiver that can
    dynamically add or remove more inputs and combine all into a single output.

    In order for `self.receive` messages to be returned in order, publisher
    will send all payloads as `OrderedPayload` msgpack encoded msgs, this
    allows our channel handler tasks to just stash the out of order payloads
    inside `self._pending_payloads` and if a in order payload is available
    signal through `self._new_payload_event`.

    On `self.receive` we wait until at least one channel is connected, then if
    an in order payload is pending, we pop and return it, in case no in order
    payload is available wait until next `self._new_payload_event.set()`.

    '''
    def __init__(
        self,
        n: trio.Nursery,

        # if connecting to a publisher that has already sent messages set 
        # to the next expected payload index this subscriber will receive
        start_index: int = 0
    ):
        self._chanmngr = ChannelManager[RingBufferReceiveChannel](
            n,
            self._open_channel,
            self._channel_task
        )

        self._schan, self._rchan = trio.open_memory_channel(0)

        self._is_closed: bool = True

        self._receive_lock = trio.StrictFIFOLock()

    @property
    def closed(self) -> bool:
        return self._is_closed

    @property
    def channels(self) -> list[ChannelInfo]:
        return self._chanmngr.channels

    def get_channel(self, name: str):
        return self._chanmngr[name]

    async def add_channel(self, name: str, must_exist: bool = False):
        await self._chanmngr.add_channel(name, must_exist=must_exist)

    async def remove_channel(self, name: str):
        await self._chanmngr.remove_channel(name)

    @acm
    async def _open_channel(

        self,
        name: str,
        must_exist: bool = False

    ) -> AsyncContextManager[RingBufferReceiveChannel]:
        '''
        Open a ringbuf through `ringd` and attach as receiver side
        '''
        async with (
            ringd.open_ringbuf(
                name=name,
                must_exist=must_exist,
            ) as token,
            attach_to_ringbuf_receiver(token) as chan
        ):
            yield chan

    async def _channel_task(self, info: ChannelInfo) -> None:
        '''
        Iterate over receive channel messages, decode them as `OrderedPayload`s
        and stash them in `self._pending_payloads`, in case we can pop next in
        order payload, signal through setting `self._new_payload_event`.

        '''
        while True:
            try:
                msg = await info.channel.receive()
                await self._schan.send(msg)

            except tractor.linux.eventfd.EFDReadCancelled as e:
                # when channel gets removed while we are doing a receive
                log.exception(e)
                break

            except trio.EndOfChannel:
                break

            except trio.ClosedResourceError:
                break

    async def receive(self) -> bytes:
        '''
        Receive next in order msg
        '''
        if self.closed:
            raise trio.ClosedResourceError

        if self._receive_lock.locked():
            raise trio.BusyResourceError

        async with self._receive_lock:
            return await self._rchan.receive()

    async def __aenter__(self):
        self._is_closed = False
        self._chanmngr.open()
        return self

    async def aclose(self) -> None:
        if self.closed:
            log.warning('tried to close RingBufferSubscriber but its already closed...')
            return

        await self._chanmngr.close()
        await self._schan.aclose()
        await self._rchan.aclose()
        self._is_closed = True

@acm
async def open_ringbuf_subscriber(

    guarantee_order: bool = False,
    force_cancel: bool = False

) -> AsyncContextManager[RingBufferPublisher]:
    '''
    Open a new ringbuf subscriber

    '''
    async with (
        trio.open_nursery() as n,
        RingBufferSubscriber(
            n,
        ) as subscriber
    ):
        if guarantee_order:
            order_receive_channel(subscriber)

        yield subscriber

        if force_cancel:
            # implicitly cancel any running channel handler task
            n.cancel_scope.cancel()
