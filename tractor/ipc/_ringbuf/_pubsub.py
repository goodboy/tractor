import time
from abc import (
    ABC,
    abstractmethod
)
from contextlib import asynccontextmanager as acm
from dataclasses import dataclass

import trio
import tractor

from tractor.ipc import (
    RingBuffBytesSender,
    attach_to_ringbuf_schannel,
    attach_to_ringbuf_rchannel
)

import tractor.ipc._ringbuf._ringd as ringd


log = tractor.log.get_logger(__name__)


@dataclass
class ChannelInfo:
    connect_time: float
    name: str
    channel: RingBuffBytesSender
    cancel_scope: trio.CancelScope


class ChannelManager(ABC):

    def __init__(
        self,
        n: trio.Nursery,
    ):
        self._n = n
        self._channels: list[ChannelInfo] = []

    @abstractmethod
    async def _channel_handler_task(self, name: str):
        ...

    def find_channel(self, name: str) -> tuple[int, ChannelInfo] | None:
        for entry in enumerate(self._channels):
            i, info = entry
            if info.name == name:
                return entry

        return None

    def _maybe_destroy_channel(self, name: str):
        maybe_entry = self.find_channel(name)
        if maybe_entry:
            i, info = maybe_entry
            info.cancel_scope.cancel()
            del self._channels[i]

    def add_channel(self, name: str):
        self._n.start_soon(
            self._channel_handler_task,
            name
        )

    def remove_channel(self, name: str):
        self._maybe_destroy_channel(name)

    def __len__(self) -> int:
        return len(self._channels)

    async def aclose(self) -> None:
        for chan in self._channels:
            self._maybe_destroy_channel(chan.name)


class RingBuffPublisher(ChannelManager, trio.abc.SendChannel[bytes]):

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

    async def _channel_handler_task(
        self,
        name: str
    ):
        async with (
            ringd.open_ringbuf(
                name=name,
                must_exist=True,
            ) as token,
            attach_to_ringbuf_schannel(token) as schan
        ):
            with trio.CancelScope() as cancel_scope:
                self._channels.append(ChannelInfo(
                    connect_time=time.time(),
                    name=name,
                    channel=schan,
                    cancel_scope=cancel_scope
                ))
                self._connect_event.set()
                await trio.sleep_forever()

        self._maybe_destroy_channel(name)

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
        await outputs.aclose()



class RingBuffSubscriber(ChannelManager, trio.abc.ReceiveChannel[bytes]):
    def __init__(
        self,
        n: trio.Nursery,
    ):
        super().__init__(n)
        self._send_chan, self._recv_chan = trio.open_memory_channel(0)

    async def _channel_handler_task(
        self,
        name: str
    ):
        async with (
            ringd.open_ringbuf(
                name=name,
                must_exist=True
            ) as token,

            attach_to_ringbuf_rchannel(token) as rchan
        ):
            with trio.CancelScope() as cancel_scope:
                self._channels.append(ChannelInfo(
                    connect_time=time.time(),
                    name=name,
                    channel=rchan,
                    cancel_scope=cancel_scope
                ))
                send_chan = self._send_chan.clone()
                try:
                    async for msg in rchan:
                        await send_chan.send(msg)

                except tractor._exceptions.InternalError:
                    ...

        self._maybe_destroy_channel(name)

    async def receive(self) -> bytes:
        return await self._recv_chan.receive()


@acm
async def open_ringbuf_subscriber():
    async with (
        trio.open_nursery() as n,
        RingBuffSubscriber(n) as inputs
    ):
        yield inputs
        await inputs.aclose()

