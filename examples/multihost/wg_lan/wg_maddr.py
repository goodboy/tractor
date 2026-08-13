# tractor: distributed structured concurrency.
r'''
Parse `wg`-tunnelled multiaddrs into `tractor`-ready addrs.

The canonical form (per py-multiaddr PR #108, verified to parse +
round-trip on that branch) nests the *overlay* endpoint **after**
the `/wg/` segment:

    /ip4/10.0.0.1/udp/51820/wg/u<key>/ip4/10.0.11.1/tcp/1616
    \_______ wg bearer ______/\_ key _/\____ tractor ep _____/
      (underlay, wg
       `ListenPort`)

- the segments *before* `/wg/` are the **bearer**: the underlay
  `(ip, udp-port)` that `wg(8)` itself listens on. Nothing in
  `tractor` ever binds this — the kernel/`wg` iface owns it.
- `/wg/u<key>` carries the tunnel peer's Curve25519 pubkey as
  multibase base64url (std base64 from `wg(8)` contains `/` and
  can't go in a `/`-delimited maddr).
- the segments *after* are the **overlay** endpoint, i.e. the
  addr `tractor` actually binds/dials. This is the only part the
  runtime sees.

XXX NOTE, `tractor`'s own `parse_maddr()` can't parse this yet
(`ValueError('Unsupported multiaddr protocol combo')`), which is
why this module exists: parse here, hand `.inner` to the runtime.

Design rules this module follows (see
`ai/tpt-backends/03_wg_tunnel_bindspace.md`):

- **parsing is pure**. `parse_wg_maddr()` does no I/O, no
  `subprocess`, no netlink. A parser that shells out is a nasty
  surprise.
- **verification is an explicit, separate step**. The caller
  composes `verify_wg_peer()` when it wants it; nothing implicit.
- **no new `Address` proto-type**. `wg` gets no entry in
  `tractor.discovery._addr._address_types` (a `bidict`, so 1:1
  proto-key<->type) bc it has no `MsgTransport` of its own. The
  tunnel is a *bindspace*, so we carry it beside the inner addr
  and strip to `.inner` at bind/dial time.

'''
from __future__ import annotations
import base64
import subprocess
from typing import Literal

import msgspec


class WGTunnelledAddr(
    msgspec.Struct,
    frozen=True,
):
    '''
    A `wg`-tunnelled endpoint: the underlay bearer, the tunnel
    peer key, and the overlay addr `tractor` binds/dials.

    '''
    # underlay, owned by `wg(8)`/the kernel — NEVER bound by us
    bearer: tuple[str, int]

    # tunnel peer pubkey in the std-base64 `wg(8)` form, i.e.
    # directly comparable to `wg show <if> peers` output
    peer_pubkey: str

    # overlay ep: an `UnwrappedAddress` as accepted by
    # `tractor.discovery.wrap_address()`
    inner: tuple[str, int]
    inner_proto: Literal['tcp'] = 'tcp'

    @property
    def maddr(self) -> str:
        '''
        Re-render the canonical maddr `str` form.

        '''
        b_host, b_port = self.bearer
        i_host, i_port = self.inner
        return (
            f'/ip4/{b_host}/udp/{b_port}'
            f'/wg/{mb_pubkey(self.peer_pubkey)}'
            f'/ip4/{i_host}/{self.inner_proto}/{i_port}'
        )


def mb_pubkey(wg8_key: str) -> str:
    '''
    `wg(8)` std-base64 pubkey -> multibase base64url (`u`-prefixed).

    '''
    import multibase
    raw: bytes = base64.b64decode(wg8_key)
    return multibase.encode('base64url', raw).decode('ascii')


def wg8_pubkey(mb_key: str) -> str:
    '''
    Inverse of `mb_pubkey()`: multibase -> `wg(8)` std-base64.

    '''
    import multibase
    raw: bytes = multibase.decode(mb_key)
    return base64.b64encode(raw).decode('ascii')


def parse_wg_maddr(
    maddr: str,
) -> WGTunnelledAddr:
    '''
    Split a `wg`-tunnelled maddr into its bearer/key/overlay
    parts. Pure — no I/O.

    Uses `py-multiaddr` when it knows the `wg` proto (PR #108),
    else falls back to a minimal segment split.

    '''
    segs: list[str] = _segments(maddr)
    try:
        wg_at: int = segs.index('wg')
    except ValueError:
        raise ValueError(
            f'Not a `wg`-tunnelled maddr, no `/wg/` segment ??\n'
            f'maddr: {maddr!r}\n'
        )

    bearer_segs: list[str] = segs[:wg_at]
    mb_key: str = segs[wg_at + 1]
    inner_segs: list[str] = segs[wg_at + 2:]

    match bearer_segs:
        case ['ip4'|'ip6', str() as b_host, 'udp', str() as b_port]:
            bearer = (b_host, int(b_port))
        case _:
            raise ValueError(
                f'Bad `wg` bearer, expected `/ip4|ip6/<h>/udp/<p>`\n'
                f'got: {"/".join(bearer_segs)!r}\n'
                f'from maddr: {maddr!r}\n'
            )

    match inner_segs:
        case ['ip4'|'ip6', str() as i_host, 'tcp', str() as i_port]:
            inner = (i_host, int(i_port))
            inner_proto = 'tcp'
        case []:
            raise ValueError(
                f'`wg` maddr declares no overlay endpoint!\n'
                f'A bare `/…/wg/<key>` names only the tunnel; '
                f'append the ep `tractor` should bind, e.g.\n'
                f'  {maddr}/ip4/10.0.11.1/tcp/1616\n'
            )
        case _:
            raise ValueError(
                f'Unsupported `wg` overlay proto combo\n'
                f'got: {"/".join(inner_segs)!r}\n'
                f'from maddr: {maddr!r}\n'
            )

    return WGTunnelledAddr(
        bearer=bearer,
        peer_pubkey=wg8_pubkey(mb_key),
        inner=inner,
        inner_proto=inner_proto,
    )


def _segments(maddr: str) -> list[str]:
    '''
    Deliver a maddr's `/`-split segments, preferring the real
    parser when it supports `wg`.

    '''
    from multiaddr import Multiaddr
    try:
        # the real thing: validates every proto + value
        Multiaddr(maddr)
    except Exception:
        # XXX STOPGAP, only until py-multiaddr#108 lands; then
        # this branch is dead and `Multiaddr` is authoritative.
        # We deliberately DON'T hand-roll a `wg` codec (the whole
        # point of gh #429 was dropping the NIH parser).
        pass
    return [s for s in maddr.split('/') if s]


def verify_wg_peer(
    addr: WGTunnelledAddr,
    iface: str = 'wg0',
) -> bool:
    '''
    True iff `addr.peer_pubkey` is a configured peer (or our own
    pubkey) on `iface`.

    IMPURE + explicit by design: never called from
    `parse_wg_maddr()`.

    ?TODO, per plan-03 layer B, swap this body for `pyroute2`
    (keeping the signature) — and note `setns(2)` is *per-thread*,
    so a query issued via `trio.to_thread` lands in the ORIGINAL
    netns unless `netns=` is passed down.

    '''
    def _wg(*args: str) -> str:
        return subprocess.run(
            ['wg', 'show', iface, *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    return (
        addr.peer_pubkey in _wg('peers').split()
        or
        addr.peer_pubkey == _wg('public-key').strip()
    )
