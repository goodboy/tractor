# tractor: distributed structured concurrency.
'''
Host A: the service host, reachable over a `wg` tunnel.

Binds `tractor`'s registrar + an `echo_srv` sub-actor on the
tunnel's *overlay* addr, declared as a single `wg` maddr.

'''

from __future__ import annotations

import os

import tractor
import trio

from wg_maddr import (
    parse_wg_maddr,
    verify_wg_key,
    WGTunnelledAddr,
)

# bearer = host A's underlay `(ip, wg ListenPort)`
# key    = host A's OWN tunnel pubkey
# overlay = the ep `tractor` binds, on the wg iface's addr
WG_MADDR: str = (
    '/ip4/192.168.1.50/udp/51820'
    '/wg/u<A_pub_b64url>'
    '/ip4/10.0.11.1/tcp/1616'
)


async def echo(msg: str) -> str:
    actor = tractor.current_actor()
    return f'{actor.aid.name!r} echoes: {msg}'


async def main():
    addr: WGTunnelledAddr = parse_wg_maddr(WG_MADDR)
    inspection: str | None = os.environ.get('WG_KEY_INSPECTION')
    if not await verify_wg_key(
        addr,
        role='local',
        inspection=inspection,
    ):
        raise RuntimeError(
            f'Maddr key is not wg0 local public key!\n'
            f'maddr: {WG_MADDR}\n'
            f'key: {addr.wg_pubkey}\n'
        )
    print(
        f'wg bearer (kernel-owned): {addr.bearer}\n'
        f'tractor overlay ep: {addr.overlay}\n'
    )
    async with tractor.open_nursery(
        # XXX only `.overlay` crosses into the runtime; the bearer
        # + key are iface-layer concerns `tractor` never binds.
        registry_addrs=[addr.overlay],
        enable_transports=[addr.overlay_proto],
    ) as an:
        await an.start_actor(
            'echo_srv',
            bind_addrs=[(addr.overlay[0], 0)],
            enable_transports=[addr.overlay_proto],
            enable_modules=['host_a_srv'],
        )
        print(f'echo_srv up on\n  {addr.maddr}\n')
        await trio.sleep_forever()


if __name__ == '__main__':
    trio.run(main)
