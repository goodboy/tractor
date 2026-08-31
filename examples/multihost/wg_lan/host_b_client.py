# tractor: distributed structured concurrency.
'''
Host B: workstation dialing host A's actor tree through the
`wg` tunnel.

'''

from __future__ import annotations

import os

import tractor
import trio

from host_a_srv import echo  # noqa: F401  (RPC refs it by mod path)
from wg_maddr import (
    parse_wg_maddr,
    verify_wg_key,
    WGTunnelledAddr,
)

# same maddr as host A: A's bearer, A's key, A's overlay ep
WG_MADDR: str = (
    '/ip4/192.168.1.50/udp/51820'
    '/wg/u<A_pub_b64url>'
    '/ip4/10.0.11.1/tcp/1616'
)
LOCAL_OVERLAY_BIND: tuple[str, int] = ('10.0.11.2', 0)


async def main():
    addr: WGTunnelledAddr = parse_wg_maddr(WG_MADDR)
    inspection: str | None = os.environ.get('WG_KEY_INSPECTION')
    if not await verify_wg_key(
        addr,
        role='peer',
        inspection=inspection,
    ):
        raise RuntimeError(
            f'Maddr key is not a configured wg0 peer!\n'
            f'maddr: {WG_MADDR}\n'
            f'key: {addr.wg_pubkey}\n'
        )
    async with (
        tractor.open_root_actor(
            name='wg_client',
            tpt_bind_addrs=[LOCAL_OVERLAY_BIND],
            registry_addrs=[addr.overlay],
            enable_transports=[addr.overlay_proto],
        ),
        tractor.find_actor(
            'echo_srv',
            registry_addrs=[addr.overlay],
            raise_on_none=True,
        ) as portal,
    ):
        res: str = await portal.run(
            echo,
            msg='hello over wg!',
        )
        print(res)


if __name__ == '__main__':
    trio.run(main)
