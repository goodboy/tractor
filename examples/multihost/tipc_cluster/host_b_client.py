'''
HOST B — dial host A's service *by name*, across the cluster.

The `.connect()` on a TIPC service name IS the discovery lookup:
the kernel resolves the published name to whichever node serves
it. So this client needs no IP, no port and no idea where host A
actually is.

Prereqs: same bearer setup as `host_a_srv.py`, and that script
already running on the other node.

    python host_b_client.py

'''
from __future__ import annotations

import trio
import tractor
from host_a_srv import echo
from tractor.ipc._tipc import (
    TIPCAddress,
    is_tipc_available,
)


async def main() -> None:
    reg: TIPCAddress = TIPCAddress.get_root()
    print(f'host B dialling {reg} (by NAME, not address)')

    async with tractor.open_root_actor(
        name='host_b',
        enable_transports=['tipc'],
        registry_addrs=[reg.unwrap()],
    ):
        async with tractor.find_actor('host_a') as ptl:
            if ptl is None:
                raise RuntimeError(
                    'No `host_a` in the cluster name table!\n'
                    ' |_is `host_a_srv.py` running?\n'
                    ' |_does `tipc link list` show the peer?\n'
                )

            # `.open_context()` derives a `NamespacePath` from a
            # callable. Importing `echo` also loads its module path
            # locally, while host A's `enable_modules` authorizes the
            # corresponding remote callable.
            async with (
                ptl.open_context(
                    echo,
                ) as (ctx, _),
                ctx.open_stream() as stream,
            ):
                for msg in ('hello', 'from', 'the other node'):
                    await stream.send(msg)
                    print(f'host-b <- {await stream.receive()!r}')


if __name__ == '__main__':
    if not is_tipc_available():
        raise RuntimeError(
            'The `tipc` kernel module is not loaded!\n'
            ' |_try: `sudo modprobe tipc`\n'
        )
    trio.run(main)
