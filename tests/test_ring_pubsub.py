from typing import AsyncContextManager
from contextlib import asynccontextmanager as acm

import trio
import pytest
import tractor

from tractor.trionics import gather_contexts

from tractor.ipc._ringbuf import open_ringbufs
from tractor.ipc._ringbuf._pubsub import (
    open_ringbuf_publisher,
    open_ringbuf_subscriber,
    get_publisher,
    get_subscriber,
    open_pub_channel_at,
    open_sub_channel_at
)


log = tractor.log.get_console_log(level='info')


@tractor.context
async def publish_range(
    ctx: tractor.Context,
    size: int
):
    pub = get_publisher()
    await ctx.started()
    for i in range(size):
        await pub.send(i.to_bytes(4))
        log.info(f'sent {i}')

    await pub.flush()

    log.info('range done')


@tractor.context
async def subscribe_range(
    ctx: tractor.Context,
    size: int
):
    sub = get_subscriber()
    await ctx.started()

    for i in range(size):
        recv = int.from_bytes(await sub.receive())
        if recv != i:
            raise AssertionError(
                f'received: {recv} expected: {i}'
            )

        log.info(f'received: {recv}')

    log.info('range done')


@tractor.context
async def subscriber_child(ctx: tractor.Context):
    try:
        async with open_ringbuf_subscriber(guarantee_order=True):
            await ctx.started()
            await trio.sleep_forever()

    finally:
        log.info('subscriber exit')


@tractor.context
async def publisher_child(
    ctx: tractor.Context,
    batch_size: int
):
    try:
        async with open_ringbuf_publisher(
            guarantee_order=True,
            batch_size=batch_size
        ):
            await ctx.started()
            await trio.sleep_forever()

    finally:
        log.info('publisher exit')


@acm
async def open_pubsub_test_actors(

    ring_names: list[str],
    size: int,
    batch_size: int

) -> AsyncContextManager[tuple[tractor.Portal, tractor.Portal]]:

    with trio.fail_after(5):
        async with tractor.open_nursery(
            enable_modules=[
                'tractor.linux._fdshare'
            ]
        ) as an:
            modules = [
                __name__,
                'tractor.linux._fdshare',
                'tractor.ipc._ringbuf._pubsub'
            ]
            sub_portal = await an.start_actor(
                'sub',
                enable_modules=modules
            )
            pub_portal = await an.start_actor(
                'pub',
                enable_modules=modules
            )

            async with (
                sub_portal.open_context(subscriber_child) as (long_rctx, _),
                pub_portal.open_context(
                    publisher_child,
                    batch_size=batch_size
                ) as (long_sctx, _),

                open_ringbufs(ring_names) as tokens,

                gather_contexts([
                    open_sub_channel_at('sub', ring)
                    for ring in tokens
                ]),
                gather_contexts([
                    open_pub_channel_at('pub', ring)
                    for ring in tokens
                ]),
                sub_portal.open_context(subscribe_range, size=size) as (rctx, _),
                pub_portal.open_context(publish_range, size=size) as (sctx, _)
            ):
                yield

                await rctx.wait_for_result()
                await sctx.wait_for_result()

                await long_sctx.cancel()
                await long_rctx.cancel()

            await an.cancel()


@pytest.mark.parametrize(
    ('ring_names', 'size', 'batch_size'),
    [
        (
            ['ring-first'],
            100,
            1
        ),
        (
            ['ring-first'],
            69,
            1
        ),
        (
            [f'multi-ring-{i}' for i in range(3)],
            1000,
            100
        ),
    ],
    ids=[
        'simple',
        'redo-simple',
        'multi-ring',
    ]
)
def test_pubsub(
    request,
    ring_names: list[str],
    size: int,
    batch_size: int
):
    async def main():
        async with open_pubsub_test_actors(
            ring_names, size, batch_size
        ):
            ...

    trio.run(main)
