# tractor: distributed structured concurrency.
'''
Host B: workstation dialing host A's actor tree through the
`wg` tunnel.

'''
from __future__ import annotations

import tractor
import trio
from tractor.discovery import (
    TunnelledAddress,
    parse_wg_maddr,
)

from host_a_srv import echo  # noqa: F401  (RPC refs it by mod path)
from wg_maddr import verify_wg_peer

# same maddr as host A: A's bearer, A's key, A's overlay ep
WG_MADDR: str = (
    '/ip4/192.168.1.50/udp/51820'
    '/wg/u<A_pub_b64url>'
    '/ip4/10.0.11.1/tcp/1616'
)


async def main():
    addr: TunnelledAddress = parse_wg_maddr(WG_MADDR)
    assert verify_wg_peer(addr), (
        f'wg pubkey from maddr not a peer on wg0 !\n'
        f'maddr: {WG_MADDR}\n'
    )
    async with (
        tractor.open_root_actor(
            name='wg_client',
            registry_addrs=[addr.overlay],
            enable_transports=[addr.overlay.proto_key],
        ),
        tractor.find_actor(
            'echo_srv',
            registry_addrs=[addr.overlay],
        ) as portal,
    ):
        res: str = await portal.run(
            echo,
            msg='hello over wg!',
        )
        print(res)


if __name__ == '__main__':
    trio.run(main)
