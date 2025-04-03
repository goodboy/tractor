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
import time
from typing import (
    runtime_checkable,
    Protocol,
    TypeVar,
    Self,
    AsyncContextManager
)
from contextlib import asynccontextmanager as acm
from dataclasses import dataclass

import trio
import tractor

from tractor.ipc import (
    RingBuffBytesSender,
    RingBuffBytesReceiver,
    attach_to_ringbuf_schannel,
    attach_to_ringbuf_rchannel
)

import tractor.ipc._ringbuf._ringd as ringd


log = tractor.log.get_logger(__name__)


ChannelType = TypeVar('ChannelType')


@dataclass
class ChannelInfo:
    connect_time: float
    name: str
    channel: ChannelType
    cancel_scope: trio.CancelScope


# TODO: maybe move this abstraction to another module or standalone?
# its not ring buf specific and allows fan out and fan in an a dynamic
# amount of channels
@runtime_checkable
class ChannelManager(Protocol[ChannelType]):
    '''
    Common data structures and methods pubsub classes use to manage channels &
    their related handler background tasks, as well as cancellation of them.

    '''

    def __init__(
        self,
        n: trio.Nursery,
    ):
        self._n = n
        self._channels: list[Self.ChannelInfo] = []

    async def _open_channel(
        self,
        name: str
    ) -> AsyncContextManager[ChannelType]:
        '''
        Used to instantiate channel resources given a name

        '''
        ...

    async def _channel_task(self, info: ChannelInfo) -> None:
        '''
        Long running task that manages the channel

        '''
        ...

    async def _channel_handler_task(self, name: str):
        async with self._open_channel(name) as chan:
            with trio.CancelScope() as cancel_scope:
                info = Self.ChannelInfo(
                    connect_time=time.time(),
                    name=name,
                    channel=chan,
                    cancel_scope=cancel_scope
                )
                self._channels.append(info)
                await self._channel_task(info)

        self._maybe_destroy_channel(name)

    def find_channel(self, name: str) -> tuple[int, ChannelInfo] | None:
        '''
        Given a channel name maybe return its index and value from
        internal _channels list.

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
        maybe_entry = self.find_channel(name)
        if maybe_entry:
            i, info = maybe_entry
            info.cancel_scope.cancel()
            del self._channels[i]

    def add_channel(self, name: str):
        '''
        Add a new channel to be handled

        '''
        self._n.start_soon(
            self._channel_handler_task,
            name
        )

    def remove_channel(self, name: str):
        '''
        Remove a channel and stop its handling

        '''
        self._maybe_destroy_channel(name)

    def __len__(self) -> int:
        return len(self._channels)

    async def aclose(self) -> None:
        for chan in self._channels:
            self._maybe_destroy_channel(chan.name)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()


class RingBuffPublisher(
    ChannelManager[RingBuffBytesSender]
):
    '''
    Implement ChannelManager protocol + trio.abc.SendChannel[bytes]
    using ring buffers as transport.

    - use a `trio.Event` to make sure `send` blocks until at least one channel
      available.

    '''

    def __init__(
        self,
        n: trio.Nursery,
        buf_size: int = 10 * 1024,
        batch_size: int = 1
    ):
        super().__init__(n)
        self._connect_event = trio.Event()
        self._next_turn: int = 0

        self._batch_size: int = batch_size

    @acm
    async def _open_channel(
        self,
        name: str
    ) -> AsyncContextManager[RingBuffBytesSender]:
        async with (
            ringd.open_ringbuf(
                name=name,
                must_exist=True,
            ) as token,
            attach_to_ringbuf_schannel(token) as chan
        ):
            yield chan

    async def _channel_task(self, info: Self.ChannelInfo) -> None:
        self._connect_event.set()
        await trio.sleep_forever()

    async def send(self, msg: bytes):
        # wait at least one decoder connected
        if len(self) == 0:
            await self._connect_event.wait()
            self._connect_event = trio.Event()

        if self._next_turn >= len(self):
            self._next_turn = 0

        turn = self._next_turn
        self._next_turn += 1

        output = self._channels[turn]
        await output.channel.send(msg)

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @batch_size.setter
    def set_batch_size(self, value: int) -> None:
        for output in self._channels:
            output.channel.batch_size = value

    async def flush(
        self,
        new_batch_size: int | None = None
    ):
        for output in self._channels:
            await output.channel.flush(
                new_batch_size=new_batch_size
            )

    async def send_eof(self):
        for output in self._channels:
            await output.channel.send_eof()


@acm
async def open_ringbuf_publisher(
    buf_size: int = 10 * 1024,
    batch_size: int = 1
):
    async with (
        trio.open_nursery() as n,
        RingBuffPublisher(
            n,
            buf_size=buf_size,
            batch_size=batch_size
        ) as outputs
    ):
        yield outputs



class RingBuffSubscriber(
    ChannelManager[RingBuffBytesReceiver]
):
    '''
    Implement ChannelManager protocol + trio.abc.ReceiveChannel[bytes]
    using ring buffers as transport.

    - use a trio memory channel pair to multiplex all received messages into a
      single `trio.MemoryReceiveChannel`, give a sender channel clone to each
      _channel_task.

    '''
    def __init__(
        self,
        n: trio.Nursery,
    ):
        super().__init__(n)
        self._send_chan, self._recv_chan = trio.open_memory_channel(0)

    @acm
    async def _open_channel(
        self,
        name: str
    ) -> AsyncContextManager[RingBuffBytesReceiver]:
        async with (
            ringd.open_ringbuf(
                name=name,
                must_exist=True,
            ) as token,
            attach_to_ringbuf_rchannel(token) as chan
        ):
            yield chan

    async def _channel_task(self, info: ChannelInfo) -> None:
        send_chan = self._send_chan.clone()
        try:
            async for msg in info.channel:
                await send_chan.send(msg)

        except tractor._exceptions.InternalError:
            # TODO: cleaner cancellation!
            ...

    async def receive(self) -> bytes:
        return await self._recv_chan.receive()


@acm
async def open_ringbuf_subscriber():
    async with (
        trio.open_nursery() as n,
        RingBuffSubscriber(n) as inputs
    ):
        yield inputs

