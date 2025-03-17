import time

import trio
import pytest

import tractor
from tractor.ipc._ringbuf import (
    open_ringbuf,
    attach_to_ringbuf_receiver,
    attach_to_ringbuf_sender,
    attach_to_ringbuf_pair,
    attach_to_ringbuf_stream,
    RBToken,
)
from tractor._testing.samples import (
    generate_single_byte_msgs,
    generate_sample_messages
)

# in case you don't want to melt your cores, uncomment dis!
pytestmark = pytest.mark.skip


@tractor.context
async def child_read_shm(
    ctx: tractor.Context,
    msg_amount: int,
    token: RBToken,
) -> None:
    recvd_bytes = 0
    await ctx.started()
    start_ts = time.time()
    async with attach_to_ringbuf_receiver(token) as receiver:
        async for msg in receiver:
            recvd_bytes += len(msg)

    end_ts = time.time()
    elapsed = end_ts - start_ts
    elapsed_ms = int(elapsed * 1000)

    print(f'\n\telapsed ms: {elapsed_ms}')
    print(f'\tmsg/sec: {int(msg_amount / elapsed):,}')
    print(f'\tbytes/sec: {int(recvd_bytes / elapsed):,}')
    print(f'\treceived bytes: {recvd_bytes}')


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
    async with attach_to_ringbuf_sender(token, cleanup=False) as sender:
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
            proc_kwargs = {'pass_fds': token.fds}

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
                        token=token,
                        msg_amount=msg_amount,
                        rand_min=rand_min,
                        rand_max=rand_max,
                    ) as (sctx, total_bytes),
                    recv_p.open_context(
                        child_read_shm,
                        token=token,
                        msg_amount=msg_amount
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
    async with attach_to_ringbuf_receiver(token) as receiver:
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
                attach_to_ringbuf_sender(token) as _sender,
            ):
                recv_p = await an.start_actor(
                    'ring_blocked_receiver',
                    enable_modules=[__name__],
                    proc_kwargs={
                        'pass_fds': token.fds
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
    async with attach_to_ringbuf_sender(token) as sender:
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
                        'pass_fds': token.fds
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
    msg = generate_single_byte_msgs(100)
    msgs = []

    async def main():
        with open_ringbuf(
            'test_ringbuf_max_bytes',
            buf_size=10
        ) as token:
            async with (
                trio.open_nursery() as n,
                attach_to_ringbuf_sender(token, cleanup=False) as sender,
                attach_to_ringbuf_receiver(token, cleanup=False) as receiver
            ):
                async def _send_and_close():
                    await sender.send_all(msg)
                    await sender.aclose()

                n.start_soon(_send_and_close)
                while len(msgs) < len(msg):
                    msg_part = await receiver.receive_some(max_bytes=1)
                    assert len(msg_part) == 1
                    msgs.append(msg_part)

    trio.run(main)
    assert msg == b''.join(msgs)


def test_stapled_ringbuf():
    '''
    Open two ringbufs and give tokens to tasks (swap them such that in/out tokens
    are inversed on each task) which will open the streams and use trio.StapledStream
    to have a single bidirectional stream.

    Then take turns to send and receive messages.

    '''
    msg = generate_single_byte_msgs(100)
    pair_0_msgs = []
    pair_1_msgs = []

    pair_0_done = trio.Event()
    pair_1_done = trio.Event()

    async def pair_0(token_in: RBToken, token_out: RBToken):
        async with attach_to_ringbuf_pair(
            token_in,
            token_out,
            cleanup_in=False,
            cleanup_out=False
        ) as stream:
            # first turn to send
            await stream.send_all(msg)

            # second turn to receive
            while len(pair_0_msgs) != len(msg):
                _msg = await stream.receive_some(max_bytes=1)
                pair_0_msgs.append(_msg)

            pair_0_done.set()
            await pair_1_done.wait()


    async def pair_1(token_in: RBToken, token_out: RBToken):
        async with attach_to_ringbuf_pair(
            token_in,
            token_out,
            cleanup_in=False,
            cleanup_out=False
        ) as stream:
            # first turn to receive
            while len(pair_1_msgs) != len(msg):
                _msg = await stream.receive_some(max_bytes=1)
                pair_1_msgs.append(_msg)

            # second turn to send
            await stream.send_all(msg)

            pair_1_done.set()
            await pair_0_done.wait()


    async def main():
        with tractor.ipc.open_ringbuf_pair(
            'test_stapled_ringbuf'
        ) as (token_0, token_1):
            async with trio.open_nursery() as n:
                n.start_soon(pair_0, token_0, token_1)
                n.start_soon(pair_1, token_1, token_0)


    trio.run(main)

    assert msg == b''.join(pair_0_msgs)
    assert msg == b''.join(pair_1_msgs)


@tractor.context
async def child_transport_sender(
    ctx: tractor.Context,
    msg_amount_min: int,
    msg_amount_max: int,
    token_in: RBToken,
    token_out: RBToken
):
    import random
    msgs, _total_bytes = generate_sample_messages(
        random.randint(msg_amount_min, msg_amount_max),
        rand_min=256,
        rand_max=1024,
    )
    async with attach_to_ringbuf_stream(
        token_in,
        token_out
    ) as transport:
        await ctx.started(msgs)

        for msg in msgs:
            await transport.send(msg)

        await transport.recv()


def test_ringbuf_transport():

    msg_amount_min = 100
    msg_amount_max = 1000

    async def main():
        with tractor.ipc.open_ringbuf_pair(
            'test_ringbuf_transport'
        ) as (token_0, token_1):
            async with (
                attach_to_ringbuf_stream(token_0, token_1) as transport,
                tractor.open_nursery() as an
            ):
                recv_p = await an.start_actor(
                    'test_ringbuf_transport_sender',
                    enable_modules=[__name__],
                    proc_kwargs={
                        'pass_fds': token_0.fds + token_1.fds
                    }
                )
                async with (
                    recv_p.open_context(
                        child_transport_sender,
                        msg_amount_min=msg_amount_min,
                        msg_amount_max=msg_amount_max,
                        token_in=token_1,
                        token_out=token_0
                    ) as (ctx, msgs),
                ):
                    recv_msgs = []
                    while len(recv_msgs) < len(msgs):
                        recv_msgs.append(await transport.recv())

                    await transport.send(b'end')
                    await recv_p.cancel_actor()
                    assert recv_msgs == msgs

    trio.run(main)
