'''
HOST A — publish a `tractor` service on a cluster-scoped TIPC
service name.

Note what is NOT in this file: any IP address, hostname or port.
The actor's address IS the service name `(stype, instance)`, and
the kernel routes it over whatever bearer you enabled. Move this
process to another node and host B's dial keeps working,
unchanged.

Prereqs on BOTH hosts (see README.md),

    sudo modprobe tipc
    sudo tipc bearer enable media eth device <iface>
    tipc link list          # must show a link to the peer

Then here,

    python host_a_srv.py

'''
from __future__ import annotations

import trio
import tractor
from tractor.ipc._tipc import (
    TIPCAddress,
    is_tipc_available,
)


@tractor.context
async def echo(
    ctx: tractor.Context,
) -> None:
    await ctx.started()
    async with ctx.open_stream() as stream:
        async for msg in stream:
            print(f'host-a <- {msg!r}')
            await stream.send(f'{msg} (from host A)')


async def main() -> None:
    # the host-singleton registrar name, `instance=1616` —
    # the same "1616 is tractor's registrar" idiom as the tcp
    # port and the `registry@1616.sock` UDS filename.
    reg: TIPCAddress = TIPCAddress.get_root()
    print(f'host A publishing {reg}')

    async with tractor.open_nursery(
        enable_transports=['tipc'],
        registry_addrs=[reg.unwrap()],
    ) as an:
        await an.start_actor(
            'host_a',
            enable_modules=[__name__],
        )
        print(
            'host_a up — `tipc nametable show` on EITHER host\n'
            'should now list this service. ctrl-c to stop.'
        )
        await trio.sleep_forever()


if __name__ == '__main__':
    if not is_tipc_available():
        raise RuntimeError(
            'The `tipc` kernel module is not loaded!\n'
            ' |_try: `sudo modprobe tipc`\n'
        )
    trio.run(main)
