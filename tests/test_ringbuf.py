import time
import hashlib

import trio
import pytest

import tractor
from tractor.ipc._ringbuf import (
    open_ringbuf,
    open_ringbuf_pair,
    attach_to_ringbuf_receiver,
    attach_to_ringbuf_sender,
    attach_to_ringbuf_channel,
    RBToken,
)
from tractor._testing.samples import (
    generate_single_byte_msgs,
    RandomBytesGenerator
)


@tractor.context
async def child_read_shm(
    ctx: tractor.Context,
    token: RBToken,
) -> str:
    '''
    Sub-actor used in `test_ringbuf`.

    Attach to a ringbuf and receive all messages until end of stream.
    Keep track of how many bytes received and also calculate 
    sha256 of the whole byte stream.

    Calculate and print performance stats, finally return calculated
    hash.

    '''
    await ctx.started()
    print('reader started')
    msg_amount = 0
    recvd_bytes = 0
    recvd_hash = hashlib.sha256()
    start_ts = time.time()
    async with attach_to_ringbuf_receiver(token) as receiver:
        async for msg in receiver:
            msg_amount += 1
            recvd_hash.update(msg)
            recvd_bytes += len(msg)

    end_ts = time.time()
    elapsed = end_ts - start_ts
    elapsed_ms = int(elapsed * 1000)

    print(f'\n\telapsed ms: {elapsed_ms}')
    print(f'\tmsg/sec: {int(msg_amount / elapsed):,}')
    print(f'\tbytes/sec: {int(recvd_bytes / elapsed):,}')
    print(f'\treceived msgs: {msg_amount:,}')
    print(f'\treceived bytes: {recvd_bytes:,}')

    return recvd_hash.hexdigest()


@tractor.context
async def child_write_shm(
    ctx: tractor.Context,
    msg_amount: int,
    rand_min: int,
    rand_max: int,
    buf_size: int
) -> None:
    '''
    Sub-actor used in `test_ringbuf`

    Generate `msg_amount` payloads with
    `random.randint(rand_min, rand_max)` random bytes at the end,
    Calculate sha256 hash and send it to parent on `ctx.started`.

    Attach to ringbuf and send all generated messages.

    '''
    rng = RandomBytesGenerator(
        msg_amount,
        rand_min=rand_min,
        rand_max=rand_max,
    )
    async with (
        open_ringbuf('test_ringbuf', buf_size=buf_size) as token,
        attach_to_ringbuf_sender(token) as sender
    ):
        await ctx.started(token)
        print('writer started')
        for msg in rng:
            await sender.send(msg)

            if rng.msgs_generated % rng.recommended_log_interval == 0:
                print(f'wrote {rng.msgs_generated} msgs')

    print('writer exit')
    return rng.hexdigest


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
    - Open `child_write_shm` ctx in sub-actor which will generate a
    random payload and send its hash on `ctx.started`, finally sending
    the payload through the stream.
    - Open `child_read_shm` ctx in sub-actor which will receive the
    payload, calculate perf stats and return the hash.
    - Compare both hashes

    '''
    async def main():
        async with tractor.open_nursery() as an:
            send_p = await an.start_actor(
                'ring_sender',
                enable_modules=[
                    __name__,
                    'tractor.linux._fdshare'
                ],
            )
            recv_p = await an.start_actor(
                'ring_receiver',
                enable_modules=[
                    __name__,
                    'tractor.linux._fdshare'
                ],
            )
            async with (
                send_p.open_context(
                    child_write_shm,
                    msg_amount=msg_amount,
                    rand_min=rand_min,
                    rand_max=rand_max,
                    buf_size=buf_size
                ) as (sctx, token),

                recv_p.open_context(
                    child_read_shm,
                    token=token,
                ) as (rctx, _),
            ):
                sent_hash = await sctx.result()
                recvd_hash = await rctx.result()

                assert sent_hash == recvd_hash

            await an.cancel()

    trio.run(main)


@tractor.context
async def child_blocked_receiver(ctx: tractor.Context):
    async with (
        open_ringbuf('test_ring_cancel_reader') as token,

        attach_to_ringbuf_receiver(token) as receiver
    ):
        await ctx.started(token)
        await receiver.receive_some()


def test_reader_cancel():
    '''
    Test that a receiver blocked on eventfd(2) read responds to
    cancellation.

    '''
    async def main():
        async with tractor.open_nursery() as an:
            recv_p = await an.start_actor(
                'ring_blocked_receiver',
                enable_modules=[
                    __name__,
                    'tractor.linux._fdshare'
                ],
            )
            async with (
                recv_p.open_context(
                    child_blocked_receiver,
                ) as (sctx, token),

                attach_to_ringbuf_sender(token),
            ):
                await trio.sleep(.1)
                await an.cancel()


    with pytest.raises(tractor._exceptions.ContextCancelled):
        trio.run(main)


@tractor.context
async def child_blocked_sender(ctx: tractor.Context):
    async with (
        open_ringbuf(
            'test_ring_cancel_sender',
            buf_size=1
        ) as token,

        attach_to_ringbuf_sender(token) as sender
    ):
        await ctx.started(token)
        await sender.send_all(b'this will wrap')


def test_sender_cancel():
    '''
    Test that a sender blocked on eventfd(2) read responds to
    cancellation.

    '''
    async def main():
        async with tractor.open_nursery() as an:
            recv_p = await an.start_actor(
                'ring_blocked_sender',
                enable_modules=[
                    __name__,
                    'tractor.linux._fdshare'
                ],
            )
            async with (
                recv_p.open_context(
                    child_blocked_sender,
                ) as (sctx, token),

                attach_to_ringbuf_receiver(token)
            ):
                await trio.sleep(.1)
                await an.cancel()


    with pytest.raises(tractor._exceptions.ContextCancelled):
        trio.run(main)


def test_receiver_max_bytes():
    '''
    Test that RingBuffReceiver.receive_some's max_bytes optional
    argument works correctly, send a msg of size 100, then
    force receive of messages with max_bytes == 1, wait until
    100 of these messages are received, then compare join of
    msgs with original message

    '''
    msg = generate_single_byte_msgs(100)
    msgs = []

    rb_common = {
        'cleanup': False,
        'is_ipc': False
    }

    async def main():
        async with (
            open_ringbuf(
                'test_ringbuf_max_bytes',
                buf_size=10,
                is_ipc=False
            ) as token,

            trio.open_nursery() as n,

            attach_to_ringbuf_sender(token, **rb_common) as sender,

            attach_to_ringbuf_receiver(token, **rb_common) as receiver
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


@tractor.context
async def child_channel_sender(
    ctx: tractor.Context,
    msg_amount_min: int,
    msg_amount_max: int,
    token_in: RBToken,
    token_out: RBToken
):
    import random
    rng = RandomBytesGenerator(
        random.randint(msg_amount_min, msg_amount_max),
        rand_min=256,
        rand_max=1024,
    )
    async with attach_to_ringbuf_channel(
        token_in,
        token_out
    ) as chan:
        await ctx.started()
        for msg in rng:
            await chan.send(msg)

        await chan.send(b'bye')
        await chan.receive()
        return rng.hexdigest


def test_channel():

    msg_amount_min = 100
    msg_amount_max = 1000

    mods = [
        __name__,
        'tractor.linux._fdshare'
    ]

    async def main():
        async with (
            tractor.open_nursery(enable_modules=mods) as an,

            open_ringbuf_pair(
                'test_ringbuf_transport'
            ) as (send_token, recv_token),

            attach_to_ringbuf_channel(send_token, recv_token) as chan,
        ):
            sender = await an.start_actor(
                'test_ringbuf_transport_sender',
                enable_modules=mods,
            )
            async with (
                sender.open_context(
                    child_channel_sender,
                    msg_amount_min=msg_amount_min,
                    msg_amount_max=msg_amount_max,
                    token_in=recv_token,
                    token_out=send_token
                ) as (ctx, _),
            ):
                recvd_hash = hashlib.sha256()
                async for msg in chan:
                    if msg == b'bye':
                        await chan.send(b'bye')
                        break

                    recvd_hash.update(msg)

                sent_hash = await ctx.result()

                assert recvd_hash.hexdigest() == sent_hash

            await an.cancel()

    trio.run(main)
