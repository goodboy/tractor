import trio
import tractor
import msgspec

from tractor.ipc import (
    attach_to_ringbuf_receiver,
    attach_to_ringbuf_sender
)
from tractor.ipc._ringbuf._pubsub import (
    open_ringbuf_publisher,
    open_ringbuf_subscriber
)

import tractor.ipc._ringbuf._ringd as ringd


log = tractor.log.get_console_log(level='info')


@tractor.context
async def recv_child(
    ctx: tractor.Context,
    ring_name: str
):
    async with (
        ringd.open_ringbuf(ring_name) as token,

        attach_to_ringbuf_receiver(token) as chan,
    ):
        await ctx.started()
        async for msg in chan:
            log.info(f'received {int.from_bytes(msg)}')


@tractor.context
async def send_child(
    ctx: tractor.Context,
    ring_name: str
):
    async with (
        ringd.open_ringbuf(ring_name) as token,

        attach_to_ringbuf_sender(token) as chan,
    ):
        await ctx.started()
        for i in range(100):
            await chan.send(i.to_bytes(4))
            log.info(f'sent {i}')



def test_ringd():
    '''
    Spawn ringd actor and two childs that access same ringbuf through ringd.

    Both will use `ringd.open_ringbuf` to allocate the ringbuf, then attach to
    them as sender and receiver.

    '''
    async def main():
        async with (
            tractor.open_nursery() as an,

            ringd.open_ringd(
                loglevel='info'
            )
        ):
            recv_portal = await an.start_actor(
                'recv',
                enable_modules=[__name__]
            )
            send_portal = await an.start_actor(
                'send',
                enable_modules=[__name__]
            )

            async with (
                recv_portal.open_context(
                    recv_child,
                    ring_name='ring'
                ) as (rctx, _),

                send_portal.open_context(
                    send_child,
                    ring_name='ring'
                ) as (sctx, _),
            ):
                ...

            await an.cancel()

    trio.run(main)


class Struct(msgspec.Struct):

    def encode(self) -> bytes:
        return msgspec.msgpack.encode(self)


class AddChannelMsg(Struct, frozen=True, tag=True):
    name: str


class RemoveChannelMsg(Struct, frozen=True, tag=True):
    name: str


class RangeMsg(Struct, frozen=True, tag=True):
    start: int
    end: int


ControlMessages = AddChannelMsg | RemoveChannelMsg | RangeMsg


@tractor.context
async def subscriber_child(ctx: tractor.Context):
    await ctx.started()
    async with (
        open_ringbuf_subscriber(guarantee_order=True) as subs,
        trio.open_nursery() as n,
        ctx.open_stream() as stream
    ):
        range_msg = None
        range_event = trio.Event()
        range_scope = trio.CancelScope()

        async def _control_listen_task():
            nonlocal range_msg, range_event
            async for msg in stream:
                msg = msgspec.msgpack.decode(msg, type=ControlMessages)
                match msg:
                    case AddChannelMsg():
                        await subs.add_channel(msg.name, must_exist=False)

                    case RemoveChannelMsg():
                        await subs.remove_channel(msg.name)

                    case RangeMsg():
                        range_msg = msg
                        range_event.set()

                await stream.send(b'ack')

            range_scope.cancel()

        n.start_soon(_control_listen_task)

        with range_scope:
            while True:
                await range_event.wait()
                range_event = trio.Event()
                for i in range(range_msg.start, range_msg.end):
                    recv = int.from_bytes(await subs.receive())
                    # if recv != i:
                    #     raise AssertionError(
                    #         f'received: {recv} expected: {i}'
                    #     )

                    log.info(f'received: {recv} expected: {i}')

                await stream.send(b'valid range')
                log.info('FINISHED RANGE')

    log.info('subscriber exit')


@tractor.context
async def publisher_child(ctx: tractor.Context):
    await ctx.started()
    async with (
        open_ringbuf_publisher(batch_size=1, guarantee_order=True) as pub,
        ctx.open_stream() as stream
    ):
        abs_index = 0
        async for msg in stream:
            msg = msgspec.msgpack.decode(msg, type=ControlMessages)
            match msg:
                case AddChannelMsg():
                    await pub.add_channel(msg.name, must_exist=True)

                case RemoveChannelMsg():
                    await pub.remove_channel(msg.name)

                case RangeMsg():
                    for i in range(msg.start, msg.end):
                        await pub.send(i.to_bytes(4))
                        log.info(f'sent {i}, index: {abs_index}')
                        abs_index += 1

            await stream.send(b'ack')

    log.info('publisher exit')



def test_pubsub():
    '''
    Spawn ringd actor and two childs that access same ringbuf through ringd.

    Both will use `ringd.open_ringbuf` to allocate the ringbuf, then attach to
    them as sender and receiver.

    '''
    async def main():
        async with (
            tractor.open_nursery(
                loglevel='info',
                # debug_mode=True,
                # enable_stack_on_sig=True
            ) as an,

            ringd.open_ringd()
        ):
            recv_portal = await an.start_actor(
                'recv',
                enable_modules=[__name__]
            )
            send_portal = await an.start_actor(
                'send',
                enable_modules=[__name__]
            )

            async with (
                recv_portal.open_context(subscriber_child) as (rctx, _),
                rctx.open_stream() as recv_stream,
                send_portal.open_context(publisher_child) as (sctx, _),
                sctx.open_stream() as send_stream,
            ):
                async def send_wait_ack(msg: bytes):
                    await recv_stream.send(msg)
                    ack = await recv_stream.receive()
                    assert ack == b'ack'

                    await send_stream.send(msg)
                    ack = await send_stream.receive()
                    assert ack == b'ack'

                async def add_channel(name: str):
                    await send_wait_ack(AddChannelMsg(name=name).encode())

                async def remove_channel(name: str):
                    await send_wait_ack(RemoveChannelMsg(name=name).encode())

                async def send_range(start: int, end: int):
                    await send_wait_ack(RangeMsg(start=start, end=end).encode())
                    range_ack = await recv_stream.receive()
                    assert range_ack == b'valid range'

                # simple test, open one channel and send 0..100 range
                ring_name = 'ring-first'
                await add_channel(ring_name)
                await send_range(0, 100)
                await remove_channel(ring_name)

                # redo
                ring_name = 'ring-redo'
                await add_channel(ring_name)
                await send_range(0, 100)
                await remove_channel(ring_name)

                # multi chan test
                ring_names = []
                for i in range(3):
                    ring_names.append(f'multi-ring-{i}')

                for name in ring_names:
                    await add_channel(name)

                await send_range(0, 300)

                for name in ring_names:
                    await remove_channel(name)

            await an.cancel()

    trio.run(main)
