# tractor: distributed structured concurrency.
'''
Host A: the service host, reachable over a `wg` tunnel.

Binds `tractor`'s registrar + an `echo_srv` sub-actor on the
tunnel's *overlay* addr, declared as a single `wg` maddr.

'''
from __future__ import annotations

import tractor
import trio

from wg_maddr import (
    parse_wg_maddr,
    verify_wg_peer,
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
    assert verify_wg_peer(addr), (
        f'wg pubkey from maddr not active on wg0 !\n'
        f'maddr: {WG_MADDR}\n'
        f'key: {addr.peer_pubkey}\n'
    )
    print(
        f'wg bearer (kernel-owned): {addr.bearer}\n'
        f'tractor overlay ep: {addr.inner}\n'
    )
    async with tractor.open_nursery(
        # XXX only `.inner` crosses into the runtime; the bearer
        # + key are iface-layer concerns `tractor` never binds.
        registry_addrs=[addr.inner],
        enable_transports=[addr.inner_proto],
    ) as an:
        await an.start_actor(
            'echo_srv',
            enable_modules=[__name__],
        )
        print(f'echo_srv up on\n  {addr.maddr}\n')
        await trio.sleep_forever()


if __name__ == '__main__':
    trio.run(main)
