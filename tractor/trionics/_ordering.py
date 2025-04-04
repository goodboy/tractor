from __future__ import annotations
from heapq import (
    heappush,
    heappop
)

import trio
import msgspec


class OrderedPayload(msgspec.Struct, frozen=True):
    index: int
    payload: bytes

    @classmethod
    def from_msg(cls, msg: bytes) -> OrderedPayload:
        return msgspec.msgpack.decode(msg, type=OrderedPayload)

    def encode(self) -> bytes:
        return msgspec.msgpack.encode(self)


def order_send_channel(
    channel: trio.abc.SendChannel[bytes],
    start_index: int = 0
):

    next_index = start_index
    send_lock = trio.StrictFIFOLock()

    channel._send = channel.send
    channel._aclose = channel.aclose

    async def send(msg: bytes):
        nonlocal next_index
        async with send_lock:
            await channel._send(
                OrderedPayload(
                    index=next_index,
                    payload=msg
                ).encode()
            )
            next_index += 1

    async def aclose():
        async with send_lock:
            await channel._aclose()

    channel.send = send
    channel.aclose = aclose


def order_receive_channel(
    channel: trio.abc.ReceiveChannel[bytes],
    start_index: int = 0
):
    next_index = start_index
    pqueue = []

    channel._receive = channel.receive

    def can_pop_next() -> bool:
        return (
            len(pqueue) > 0
            and
            pqueue[0][0] == next_index
        )

    async def drain_to_heap():
        while not can_pop_next():
            msg = await channel._receive()
            msg = OrderedPayload.from_msg(msg)
            heappush(pqueue, (msg.index, msg.payload))

    def pop_next():
        nonlocal next_index
        _, msg = heappop(pqueue)
        next_index += 1
        return msg

    async def receive() -> bytes:
        if can_pop_next():
            return pop_next()

        await drain_to_heap()

        return pop_next()

    channel.receive = receive
