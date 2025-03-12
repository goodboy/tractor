"""
Shared mem primitives and APIs.

"""
import time
import uuid
import string
import random

# import numpy
import pytest
import trio
import tractor
from tractor._shm import (
    open_shm_list,
    attach_shm_list,
    EventFD, open_ringbuffer_sender, open_ringbuffer_receiver,
)


@tractor.context
async def child_attach_shml_alot(
    ctx: tractor.Context,
    shm_key: str,
) -> None:

    await ctx.started(shm_key)

    # now try to attach a boatload of times in a loop..
    for _ in range(1000):
        shml = attach_shm_list(
            key=shm_key,
            readonly=False,
        )
        assert shml.shm.name == shm_key
        await trio.sleep(0.001)


def test_child_attaches_alot():
    async def main():
        async with tractor.open_nursery() as an:

            # allocate writeable list in parent
            key = f'shml_{uuid.uuid4()}'
            shml = open_shm_list(
                key=key,
            )

            portal = await an.start_actor(
                'shm_attacher',
                enable_modules=[__name__],
            )

            async with (
                portal.open_context(
                    child_attach_shml_alot,
                    shm_key=shml.key,
                ) as (ctx, start_val),
            ):
                assert start_val == key
                await ctx.result()

            await portal.cancel_actor()

    trio.run(main)


@tractor.context
async def child_read_shm_list(
    ctx: tractor.Context,
    shm_key: str,
    use_str: bool,
    frame_size: int,
) -> None:

    # attach in child
    shml = attach_shm_list(
        key=shm_key,
        # dtype=str if use_str else float,
    )
    await ctx.started(shml.key)

    async with ctx.open_stream() as stream:
        async for i in stream:
            print(f'(child): reading shm list index: {i}')

            if use_str:
                expect = str(float(i))
            else:
                expect = float(i)

            if frame_size == 1:
                val = shml[i]
                assert expect == val
                print(f'(child): reading value: {val}')
            else:
                frame = shml[i - frame_size:i]
                print(f'(child): reading frame: {frame}')


@pytest.mark.parametrize(
    'use_str',
    [False, True],
    ids=lambda i: f'use_str_values={i}',
)
@pytest.mark.parametrize(
    'frame_size',
    [1, 2**6, 2**10],
    ids=lambda i: f'frame_size={i}',
)
def test_parent_writer_child_reader(
    use_str: bool,
    frame_size: int,
):

    async def main():
        async with tractor.open_nursery(
            # debug_mode=True,
        ) as an:

            portal = await an.start_actor(
                'shm_reader',
                enable_modules=[__name__],
                debug_mode=True,
            )

            # allocate writeable list in parent
            key = 'shm_list'
            seq_size = int(2 * 2 ** 10)
            shml = open_shm_list(
                key=key,
                size=seq_size,
                dtype=str if use_str else float,
                readonly=False,
            )

            async with (
                portal.open_context(
                    child_read_shm_list,
                    shm_key=key,
                    use_str=use_str,
                    frame_size=frame_size,
                ) as (ctx, sent),

                ctx.open_stream() as stream,
            ):

                assert sent == key

                for i in range(seq_size):

                    val = float(i)
                    if use_str:
                        val = str(val)

                    # print(f'(parent): writing {val}')
                    shml[i] = val

                    # only on frame fills do we
                    # signal to the child that a frame's
                    # worth is ready.
                    if (i % frame_size) == 0:
                        print(f'(parent): signalling frame full on {val}')
                        await stream.send(i)
                else:
                    print(f'(parent): signalling final frame on {val}')
                    await stream.send(i)

            await portal.cancel_actor()

    trio.run(main)


def random_string(size=256):
    return ''.join(random.choice(string.ascii_lowercase) for i in range(size))


async def child_read_shm(
    msg_amount: int,
    key: str,
    write_event_fd: int,
    wrap_event_fd: int,
    max_bytes: int,
) -> None:
    log = tractor.log.get_console_log(level='info')
    recvd_msgs = 0
    start_ts = time.time()
    async with open_ringbuffer_receiver(
        write_event_fd,
        wrap_event_fd,
        key,
        max_bytes=max_bytes
    ) as receiver:
        while recvd_msgs < msg_amount:
            msg = await receiver.receive_some()
            msgs = bytes(msg).split(b'\n')
            first = msgs[0]
            last = msgs[-2]
            log.info((receiver.ptr - len(msg), receiver.ptr, first[:10], last[:10]))
            recvd_msgs += len(msgs)

    end_ts = time.time()
    elapsed = end_ts - start_ts
    elapsed_ms = int(elapsed * 1000)
    log.info(f'elapsed ms: {elapsed_ms}')
    log.info(f'msg/sec: {int(msg_amount / elapsed):,}')
    log.info(f'bytes/sec: {int(max_bytes / elapsed):,}')

def test_ring_buff():
    log = tractor.log.get_console_log(level='info')
    msg_amount = 100_000
    log.info(f'generating {msg_amount} messages...')
    msgs = [
        f'[{i:08}]: {random_string()}\n'.encode('utf-8')
        for i in range(msg_amount)
    ]
    buf_size = sum((len(m) for m in msgs))
    log.info(f'done! buffer size: {buf_size}')
    async def main():
        with (
            EventFD(initval=0) as write_event,
            EventFD(initval=0) as wrap_event,
        ):
            async with (
                tractor.open_nursery() as an,
                open_ringbuffer_sender(
                    write_event.fd,
                    wrap_event.fd,
                    max_bytes=buf_size
                ) as sender
            ):
                await an.run_in_actor(
                    child_read_shm,
                    msg_amount=msg_amount,
                    key=sender.key,
                    write_event_fd=write_event.fd,
                    wrap_event_fd=wrap_event.fd,
                    max_bytes=buf_size,
                    proc_kwargs={
                        'pass_fds': (write_event.fd, wrap_event.fd)
                    }
                )
                for msg in msgs:
                    await sender.send_all(msg)


    trio.run(main)
