import time

import trio
import pytest
import tractor
from tractor._shm import (
    EFD_NONBLOCK,
    open_eventfd,
    RingBuffSender,
    RingBuffReceiver
)
from tractor._testing.samples import generate_sample_messages


@tractor.context
async def child_read_shm(
    ctx: tractor.Context,
    msg_amount: int,
    shm_key: str,
    write_eventfd: int,
    wrap_eventfd: int,
    buf_size: int,
    total_bytes: int,
    flags: int = 0,
) -> None:
    recvd_bytes = 0
    await ctx.started()
    start_ts = time.time()
    async with RingBuffReceiver(
        shm_key,
        write_eventfd,
        wrap_eventfd,
        buf_size=buf_size,
        flags=flags
    ) as receiver:
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
    shm_key: str,
    write_eventfd: int,
    wrap_eventfd: int,
    buf_size: int,
) -> None:
    msgs, total_bytes = generate_sample_messages(
        msg_amount,
        rand_min=rand_min,
        rand_max=rand_max,
    )
    await ctx.started(total_bytes)
    async with RingBuffSender(
        shm_key,
        write_eventfd,
        wrap_eventfd,
        buf_size=buf_size
    ) as sender:
        for msg in msgs:
            await sender.send_all(msg)


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
def test_ring_buff(
    msg_amount: int,
    rand_min: int,
    rand_max: int,
    buf_size: int
):
    write_eventfd = open_eventfd()
    wrap_eventfd = open_eventfd()

    proc_kwargs = {
        'pass_fds': (write_eventfd, wrap_eventfd)
    }

    shm_key = 'test_ring_buff'

    common_kwargs = {
        'msg_amount': msg_amount,
        'shm_key': shm_key,
        'write_eventfd': write_eventfd,
        'wrap_eventfd': wrap_eventfd,
        'buf_size': buf_size
    }

    async def main():
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
    shm_key: str,
    write_eventfd: int,
    wrap_eventfd: int,
    flags: int = 0
):
    async with RingBuffReceiver(
        shm_key,
        write_eventfd,
        wrap_eventfd,
        flags=flags
    ) as receiver:
        await ctx.started()
        await receiver.receive_some()


def test_ring_reader_cancel():
    flags = EFD_NONBLOCK
    write_eventfd = open_eventfd(flags=flags)
    wrap_eventfd = open_eventfd()

    proc_kwargs = {
        'pass_fds': (write_eventfd, wrap_eventfd)
    }

    shm_key = 'test_ring_cancel'

    async def main():
        async with (
            tractor.open_nursery() as an,
            RingBuffSender(
                shm_key,
                write_eventfd,
                wrap_eventfd,
            ) as _sender,
        ):
            recv_p = await an.start_actor(
                'ring_blocked_receiver',
                enable_modules=[__name__],
                proc_kwargs=proc_kwargs
            )
            async with (
                recv_p.open_context(
                    child_blocked_receiver,
                    write_eventfd=write_eventfd,
                    wrap_eventfd=wrap_eventfd,
                    shm_key=shm_key,
                    flags=flags
                ) as (sctx, _sent),
            ):
                await trio.sleep(1)
                await an.cancel()


    with pytest.raises(tractor._exceptions.ContextCancelled):
        trio.run(main)
