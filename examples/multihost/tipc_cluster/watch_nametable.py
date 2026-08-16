'''
Watch `tractor` actors (de)register themselves, live, via TIPC's
topology service.

`open_topology_events()` subscribes to the kernel's name table
and yields a `trio` receive-channel of `publish`/`withdraw`
events. That's **push-based** service discovery: no registrar
round-trip, no polling — the kernel tells you the instant any
actor anywhere in the cluster comes or goes.

Run,

    sudo modprobe tipc
    python watch_nametable.py

'''
from __future__ import annotations

import trio
import tractor
from tractor.ipc._tipc import (
    TIPCNameEvent,
    is_tipc_available,
    open_topology_events,
)


@tractor.context
async def wait_until_cancelled(
    ctx: tractor.Context,
) -> None:
    await ctx.started()
    await trio.sleep_forever()


async def print_events(
    events: trio.MemoryReceiveChannel[TIPCNameEvent],
) -> None:
    glyphs: dict[str, str] = {
        'published': '[+]',
        'withdrawn': '[-]',
        'timeout': '[!]',
    }
    async for ev in events:
        print(
            f'  {glyphs.get(ev.kind, "[?]")} {ev.kind:<10} '
            f'instance={ev.addr._instance:<12} '
            f'port=0x{ev.node:08x}:{ev.ref}'
        )


async def main() -> None:
    # NOTE, subscribe BEFORE booting the runtime so we catch the
    # root actor's own publication too.
    async with open_topology_events() as events:
        async with trio.open_nursery() as tn:
            tn.start_soon(print_events, events)

            print('watching the TIPC name table..\n')
            async with tractor.open_nursery(
                enable_transports=['tipc'],
            ) as an:
                await trio.sleep(0.3)

                print('\nspawning subactors..')
                portals: list[tractor.Portal] = []
                for name in ('donny', 'walter', 'dude'):
                    portals.append(
                        await an.start_actor(
                            name,
                            enable_modules=[__name__],
                        )
                    )
                    await trio.sleep(0.2)

                print('\ntearing down..')
                for ptl in portals:
                    await ptl.cancel_actor()
                    await trio.sleep(0.2)

                await an.cancel()

            await trio.sleep(0.5)
            tn.cancel_scope.cancel()


if __name__ == '__main__':
    if not is_tipc_available():
        raise RuntimeError(
            'The `tipc` kernel module is not loaded!\n'
            ' |_try: `sudo modprobe tipc`\n'
        )
    trio.run(main)
