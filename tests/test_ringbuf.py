import time

import trio
import pytest

import tractor
from tractor.ipc._ringbuf import (
    open_ringbuf,
    RBToken,
    RingBuffSender,
    RingBuffReceiver
)
from tractor._testing.samples import (
    generate_sample_messages,
)

# in case you don't want to melt your cores, uncomment dis!
pytestmark = pytest.mark.skip


@tractor.context
async def child_read_shm(
    ctx: tractor.Context,
    msg_amount: int,
    token: RBToken,
    total_bytes: int,
) -> None:
    recvd_bytes = 0
    await ctx.started()
    start_ts = time.time()
    async with RingBuffReceiver(token) as receiver:
        while recvd_bytes < total_bytes:
            msg = await receiver.receive_some()
            recvd_bytes += len(msg)

        # make sure we dont hold any memoryviews
        # before the ctx manager aclose()
        msg = None

    end_ts = time.time()
    elapsed = end_ts - start_ts
    elapsed_ms = int(elapsed * 1000)

    print(f'\n\telapsed ms: {elapsed_ms}')
    print(f'\tmsg/sec: {int(msg_amount / elapsed):,}')
    print(f'\tbytes/sec: {int(recvd_bytes / elapsed):,}')


@tractor.context
async def child_write_shm(
    ctx: tractor.Context,
    msg_amount: int,
    rand_min: int,
    rand_max: int,
    token: RBToken,
) -> None:
    msgs, total_bytes = generate_sample_messages(
        msg_amount,
        rand_min=rand_min,
        rand_max=rand_max,
    )
    await ctx.started(total_bytes)
    async with RingBuffSender(token) as sender:
        for msg in msgs:
            await sender.send_all(msg)

    print('writer exit')


@pytest.mark.parametrize(
    'msg_amount,rand_min,rand_max,buf_size',
    [
        # simple case, fixed payloads, large buffer
        (100_000, 0, 0, 10 * 1024),

        # guaranteed wrap around on every write
        (100, 10 * 1024, 20 * 1024, 10 * 1024),

        # large payload size, but large buffer
        (10_000, 256 * 1024, 512 * 1024, 10 * 1024 * 1024)
    ],
    ids=[
        'fixed_payloads_large_buffer',
        'wrap_around_every_write',
        'large_payloads_large_buffer',
    ]
)
def test_ringbuf(
    msg_amount: int,
    rand_min: int,
    rand_max: int,
    buf_size: int
):
    '''
    - Open a new ring buf on root actor
    - Create a sender subactor and generate {msg_amount} messages
    optionally with a random amount of bytes at the end of each,
    return total_bytes on `ctx.started`, then send all messages
    - Create a receiver subactor and receive until total_bytes are
    read, print simple perf stats.

    '''
    async def main():
        with open_ringbuf(
            'test_ringbuf',
            buf_size=buf_size
        ) as token:
            proc_kwargs = {
                'pass_fds': (token.write_eventfd, token.wrap_eventfd)
            }

            common_kwargs = {
                'msg_amount': msg_amount,
                'token': token,
            }
            async with tractor.open_nursery() as an:
                send_p = await an.start_actor(
                    'ring_sender',
                    enable_modules=[__name__],
                    proc_kwargs=proc_kwargs
                )
                recv_p = await an.start_actor(
                    'ring_receiver',
                    enable_modules=[__name__],
                    proc_kwargs=proc_kwargs
                )
                async with (
                    send_p.open_context(
                        child_write_shm,
                        rand_min=rand_min,
                        rand_max=rand_max,
                        **common_kwargs
                    ) as (sctx, total_bytes),
                    recv_p.open_context(
                        child_read_shm,
                        **common_kwargs,
                        total_bytes=total_bytes,
                    ) as (sctx, _sent),
                ):
                    await recv_p.result()

                await send_p.cancel_actor()
                await recv_p.cancel_actor()


    trio.run(main)


@tractor.context
async def child_blocked_receiver(
    ctx: tractor.Context,
    token: RBToken
):
    async with RingBuffReceiver(token) as receiver:
        await ctx.started()
        await receiver.receive_some()


def test_ring_reader_cancel():
    '''
    Test that a receiver blocked on eventfd(2) read responds to
    cancellation.

    '''
    async def main():
        with open_ringbuf('test_ring_cancel_reader') as token:
            async with (
                tractor.open_nursery() as an,
                RingBuffSender(token) as _sender,
            ):
                recv_p = await an.start_actor(
                    'ring_blocked_receiver',
                    enable_modules=[__name__],
                    proc_kwargs={
                        'pass_fds': (token.write_eventfd, token.wrap_eventfd)
                    }
                )
                async with (
                    recv_p.open_context(
                        child_blocked_receiver,
                        token=token
                    ) as (sctx, _sent),
                ):
                    await trio.sleep(1)
                    await an.cancel()


    with pytest.raises(tractor._exceptions.ContextCancelled):
        trio.run(main)


@tractor.context
async def child_blocked_sender(
    ctx: tractor.Context,
    token: RBToken
):
    async with RingBuffSender(token) as sender:
        await ctx.started()
        await sender.send_all(b'this will wrap')


def test_ring_sender_cancel():
    '''
    Test that a sender blocked on eventfd(2) read responds to
    cancellation.

    '''
    async def main():
        with open_ringbuf(
            'test_ring_cancel_sender',
            buf_size=1
        ) as token:
            async with tractor.open_nursery() as an:
                recv_p = await an.start_actor(
                    'ring_blocked_sender',
                    enable_modules=[__name__],
                    proc_kwargs={
                        'pass_fds': (token.write_eventfd, token.wrap_eventfd)
                    }
                )
                async with (
                    recv_p.open_context(
                        child_blocked_sender,
                        token=token
                    ) as (sctx, _sent),
                ):
                    await trio.sleep(1)
                    await an.cancel()


    with pytest.raises(tractor._exceptions.ContextCancelled):
        trio.run(main)


def test_ringbuf_max_bytes():
    '''
    Test that RingBuffReceiver.receive_some's max_bytes optional
    argument works correctly, send a msg of size 100, then
    force receive of messages with max_bytes == 1, wait until
    100 of these messages are received, then compare join of
    msgs with original message

    '''
    msg = b''.join(str(i % 10).encode() for i in range(100))
    msgs = []

    async def main():
        with open_ringbuf(
            'test_ringbuf_max_bytes',
            buf_size=10
        ) as token:
            async with (
                trio.open_nursery() as n,
                RingBuffSender(token, is_ipc=False) as sender,
                RingBuffReceiver(token, is_ipc=False) as receiver
            ):
                n.start_soon(sender.send_all, msg)
                while len(msgs) < len(msg):
                    msg_part = await receiver.receive_some(max_bytes=1)
                    msg_part = bytes(msg_part)
                    assert len(msg_part) == 1
                    msgs.append(msg_part)

    trio.run(main)
    assert msg == b''.join(msgs)
