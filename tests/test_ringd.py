import trio
import tractor

from tractor.ipc import (
    attach_to_ringbuf_rchannel,
    attach_to_ringbuf_schannel
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

        attach_to_ringbuf_rchannel(token) as chan,
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

        attach_to_ringbuf_schannel(token) as chan,
    ):
        await ctx.started()
        for i in range(100):
            await chan.send(i.to_bytes(4))
            log.info(f'sent {i}')



def test_ringd():
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
                await rctx.wait_for_result()
                await sctx.wait_for_result()

            await an.cancel()

    trio.run(main)
