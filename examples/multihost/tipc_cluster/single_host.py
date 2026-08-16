'''
`tractor` over `AF_TIPC` on a single host.

Every actor's IPC address is a TIPC *service name*, and binding
one publishes it into the kernel's cluster-wide name table. So
`tipc nametable show` lists your live actor tree — no registrar
query, no `tractor` API, just the kernel telling you what's up.

Run,

    sudo modprobe tipc
    python single_host.py

'''
from __future__ import annotations
import subprocess

import trio
import tractor
from tractor.ipc._tipc import (
    TRACTOR_STYPE,
    is_tipc_available,
)


def show_nametable(tag: str) -> None:
    '''
    Dump the kernel name-table rows belonging to `tractor`.

    '''
    print(f'\n--- `tipc nametable show` :: {tag} ---')
    out = subprocess.run(
        ['tipc', 'nametable', 'show'],
        capture_output=True,
        text=True,
    )
    for line in out.stdout.splitlines():
        # header, or one of *our* service-type rows
        if (
            line.startswith('Type')
            or
            line.startswith(str(TRACTOR_STYPE))
        ):
            print(f'  {line}')


@tractor.context
async def wait_until_cancelled(
    ctx: tractor.Context,
) -> None:
    await ctx.started()
    await trio.sleep_forever()


async def main() -> None:
    async with tractor.open_nursery(
        enable_transports=['tipc'],
    ) as an:

        show_nametable('root only')

        portals: list[tractor.Portal] = []
        for name in ('donny', 'walter', 'dude'):
            portals.append(
                await an.start_actor(
                    name,
                    enable_modules=[__name__],
                )
            )

        async with trio.open_nursery() as tn:
            for ptl in portals:
                tn.start_soon(
                    _hold_open,
                    ptl,
                )
            await trio.sleep(0.5)

            # XXX the money shot: 4 actors, 4 published names
            show_nametable('root + 3 subactors')

            tn.cancel_scope.cancel()

        await an.cancel()

    show_nametable('after teardown (all withdrawn)')


async def _hold_open(
    ptl: tractor.Portal,
) -> None:
    async with ptl.open_context(
        wait_until_cancelled,
    ) as (ctx, _):
        await trio.sleep_forever()


if __name__ == '__main__':
    if not is_tipc_available():
        raise RuntimeError(
            'The `tipc` kernel module is not loaded!\n'
            ' |_try: `sudo modprobe tipc`\n'
        )
    trio.run(main)
