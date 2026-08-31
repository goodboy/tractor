# tractor: distributed structured concurrency.
r'''
Parse `wg`-tunnelled multiaddrs into `tractor`-ready addrs.

The canonical form (per py-multiaddr #108, verified against its
upstream merge) nests the *overlay* endpoint **after** the `/wg/`
segment:

    /ip4/10.0.0.1/udp/51820/wg/u<key>/ip4/10.0.11.1/tcp/1616
    \_______ wg bearer ______/\_ key _/\____ tractor ep _____/
      (underlay, wg
       `ListenPort`)

Naming follows `py-multiaddr`'s own encapsulation model, where
earlier segments *wrap* later ones (`.encapsulate()` appends), so
the two roles are:

- **bearer**: the segs *before* `/wg/`, i.e. the underlay
  `(ip, udp-port)` that `wg(8)` itself listens on. Nothing in
  `tractor` ever binds this — the kernel/`wg` iface owns it.
- **overlay**: the segs *after*, i.e. the addr `tractor` actually
  binds/dials. The only part the runtime ever sees.

We deliberately avoid `inner`/`outer` for these two: in a *call*
stack "inner" reads as higher-up and later-called, whereas here
the encapsulated addr is bound *first* and sits deeper in the
maddr — two opposite intuitions on one word.

`/wg/u<key>` itself carries a declared Curve25519 pubkey
as multibase base64url (std base64 from `wg(8)` contains `/` and
so can't go in a `/`-delimited maddr). It binds nothing at all;
it's an identity, verified out-of-band.

XXX NOTE, `tractor`'s own `parse_maddr()` can't parse this yet
(`ValueError('Unsupported multiaddr protocol combo')`), which is
why this module exists: peel here, hand `.overlay` to the
runtime.

Design rules this module follows (see
`ai/tpt-backends/03_wg_tunnel_bindspace.md`):

- **let `py-multiaddr` do the parsing**. Every peel/compose goes
  through `.decapsulate_code()`, `.split()`, `.join()`,
  `.encapsulate()` and `.value_for_protocol()`. We hand-roll no
  segment splitting whatsoever — the whole point of gh #429 was
  dropping the NIH parser, and that applies to peeling this
  composed maddr every bit as much as to decoding one proto.
- **parsing is pure**. `parse_wg_maddr()` does no I/O, no
  `subprocess`, no netlink. A parser that shells out is a nasty
  surprise.
- **verification is an explicit, separate step**. The caller
  composes `verify_wg_key()` when it has permission to inspect
  the iface; nothing implicit.
- **exactly one `wg` segment is supported**. `WGTunnelledAddr`
  stores one bearer and one key, so accepting another segment
  would silently misrepresent the maddr.
- **no new `Address` proto-type**. `wg` gets no entry in
  `tractor.discovery._addr._address_types`, which maps available
  transport keys to concrete address types, bc it has no
  `MsgTransport` of its own. The
  tunnel is a *bindspace*, so we carry it beside the overlay
  addr and strip to `.overlay` at bind/dial time.

'''

from __future__ import annotations
import base64
from typing import Literal

import msgspec
from multiaddr import Multiaddr
import trio


IPProto = Literal['ip4', 'ip6']


class WGTunnelledAddr(
    msgspec.Struct,
    frozen=True,
):
    '''
    A `wg`-tunnelled endpoint: the underlay bearer, the tunnel
    key, and the overlay addr `tractor` binds/dials.

    '''

    # underlay, owned by `wg(8)`/the kernel — NEVER bound by us
    bearer: tuple[str, int]

    # declared wg pubkey in std-base64 `wg(8)` form; it is the
    # local key on the bearer host and a configured peer on a dialer
    wg_pubkey: str

    # overlay ep: an `UnwrappedAddress` as accepted by
    # `tractor.discovery.wrap_address()`
    overlay: tuple[str, int]
    overlay_proto: Literal['tcp'] = 'tcp'

    # kept so `.as_multiaddr()` re-renders the same ip family it
    # was parsed from, rather than assuming v4
    bearer_ip: IPProto = 'ip4'
    overlay_ip: IPProto = 'ip4'

    def as_multiaddr(self) -> Multiaddr:
        '''
        Re-compose the canonical `Multiaddr`, bearer outward-in,
        using `.encapsulate()` exactly as py-multiaddr's own
        tunneling example does.

        '''
        b_host, b_port = self.bearer
        o_host, o_port = self.overlay
        return (
            Multiaddr(f'/{self.bearer_ip}/{b_host}/udp/{b_port}')
            .encapsulate(
                Multiaddr(f'/wg/{mb_pubkey(self.wg_pubkey)}')
            )
            .encapsulate(
                Multiaddr(
                    f'/{self.overlay_ip}/{o_host}'
                    f'/{self.overlay_proto}/{o_port}'
                )
            )
        )

    @property
    def maddr(self) -> str:
        '''
        The canonical maddr `str` form.

        '''
        return str(self.as_multiaddr())


def mb_pubkey(wg8_key: str) -> str:
    '''
    `wg(8)` std-base64 pubkey -> multibase base64url (`u`-prefixed).

    '''
    import multibase

    raw: bytes = base64.b64decode(wg8_key, validate=True)
    if len(raw) != 32:
        raise ValueError(
            f'WireGuard public keys must decode to 32 bytes, '
            f'not {len(raw)}'
        )
    return multibase.encode('base64url', raw).decode('ascii')


def wg8_pubkey(mb_key: str) -> str:
    '''
    Inverse of `mb_pubkey()`: multibase -> `wg(8)` std-base64.

    '''
    import multibase

    raw: bytes = multibase.decode(mb_key)
    if len(raw) != 32:
        raise ValueError(
            f'WireGuard public keys must decode to 32 bytes, '
            f'not {len(raw)}'
        )
    return base64.b64encode(raw).decode('ascii')


_wg_proto_known: bool | None = None


def _have_wg_maddr_proto() -> bool:
    '''
    True iff the installed `py-multiaddr` knows the `/wg/` proto,
    i.e. carries py-multiaddr#108.

    Merged upstream 2026-07-28 (`f86519da`) but in no release as
    of `0.2.0`, hence the `[tool.uv.sources]` `rev` pin in
    `pyproject.toml`.

    Pure predicate; result cached since it can't change without a
    reinstall.

    '''
    global _wg_proto_known
    if _wg_proto_known is None:
        from multiaddr.protocols import protocol_with_name
        from multiaddr.exceptions import ProtocolNotFoundError

        try:
            protocol_with_name('wg')
            _wg_proto_known = True
        except ProtocolNotFoundError:
            _wg_proto_known = False

    return _wg_proto_known


def parse_wg_maddr(
    maddr: str | Multiaddr,
) -> WGTunnelledAddr:
    '''
    Peel a `wg`-tunnelled maddr into its bearer/key/overlay
    parts. Pure — no I/O.

    Every cut is made by `py-multiaddr`, so a malformed maddr
    (incl. a `wg` key that isn't exactly 32B) raises out of
    `Multiaddr()` rather than yielding a struct quietly built
    from garbage segs.

    '''
    if not _have_wg_maddr_proto():
        raise RuntimeError(
            f'Installed `py-multiaddr` has no `/wg/` proto!\n'
            f'Needs py-multiaddr#108, merged upstream but not\n'
            f'yet released; a `uv sync` picks up the pinned rev.\n'
            f'maddr: {maddr!r}\n'
        )

    ma: Multiaddr = (
        maddr
        if isinstance(maddr, Multiaddr)
        else Multiaddr(maddr)
    )
    segs: list[Multiaddr] = ma.split()
    names: list[str] = [
        proto.name
        for seg in segs
        for proto in seg.protocols()
    ]
    wg_count: int = names.count('wg')
    if not wg_count:
        raise ValueError(
            f'Not a `wg`-tunnelled maddr, no `/wg/` segment ??\n'
            f'maddr: {ma}\n'
        )
    if wg_count > 1:
        raise ValueError(
            f'Nested `wg` segments are not supported; '
            f'`WGTunnelledAddr` stores one tunnel only.\n'
            f'maddr: {ma}\n'
        )

    # Resolve the unreleased protocol only after the capability
    # check, so importing this module works with released multiaddr.
    from multiaddr.protocols import protocol_with_name

    wg_code: int = protocol_with_name('wg').code

    # NOTE, `.decapsulate_code()` cuts at the LAST occurrence of
    # the proto and keeps the *prefix*, which is exactly the
    # bearer. It handles `/wg/` cleanly precisely bc it cuts on
    # proto-code and never tries to match an addr value — the
    # key seg has no addr of its own.
    bearer_ma: Multiaddr = ma.decapsulate_code(wg_code)
    overlay_ma: Multiaddr = Multiaddr.join(
        *segs[names.index('wg') + 1:]
    )

    match [proto.name for proto in bearer_ma.protocols()]:
        case [('ip4' | 'ip6') as b_ip, 'udp']:
            bearer = (
                bearer_ma.value_for_protocol(b_ip),
                int(bearer_ma.value_for_protocol('udp')),
            )
        case _:
            raise ValueError(
                f'Bad `wg` bearer, expected `/ip4|ip6/<h>/udp/<p>`\n'
                f'got: {bearer_ma}\n'
                f'from maddr: {ma}\n'
            )

    match [proto.name for proto in overlay_ma.protocols()]:
        case [('ip4' | 'ip6') as o_ip, ('tcp') as l4]:
            overlay = (
                overlay_ma.value_for_protocol(o_ip),
                int(overlay_ma.value_for_protocol(l4)),
            )
        case []:
            raise ValueError(
                f'`wg` maddr declares no overlay endpoint!\n'
                f'A bare `/…/wg/<key>` names only the tunnel; '
                f'append the ep `tractor` should bind, e.g.\n'
                f'  {ma}/ip4/10.0.11.1/tcp/1616\n'
            )
        case _:
            raise ValueError(
                f'Unsupported `wg` overlay proto combo\n'
                f'got: {overlay_ma}\n'
                f'from maddr: {ma}\n'
            )

    return WGTunnelledAddr(
        bearer=bearer,
        wg_pubkey=wg8_pubkey(ma.value_for_protocol('wg')),
        overlay=overlay,
        overlay_proto=l4,
        bearer_ip=b_ip,
        overlay_ip=o_ip,
    )


async def verify_wg_key(
    addr: WGTunnelledAddr,
    role: Literal['local', 'peer'],
    iface: str = 'wg0',
    timeout: float = 5,
    inspection: str | None = None,
) -> bool:
    '''
    Verify the declared key in the role required on this host.

    A bearer host uses `role='local'`; a dialer uses `role='peer'`.
    This verifies only key presence. It does not inspect the peer's
    endpoint, AllowedIPs, handshake state, or iface addresses.

    `inspection` accepts output captured by a separate privileged
    `wg show` step. Without it, query asynchronously for callers
    which already have interface-inspection permission.

    IMPURE + explicit by design: never called from
    `parse_wg_maddr()`.

    ?TODO, per plan-03 layer B, swap this body for `pyroute2`
    (keeping the signature) — and note `setns(2)` is *per-thread*,
    so a query issued via `trio.to_thread` lands in the ORIGINAL
    netns unless `netns=` is passed down.

    '''
    match role:
        case 'local':
            field = 'public-key'
        case 'peer':
            field = 'peers'
        case _:
            raise ValueError(f'Unknown WireGuard key role: {role!r}')

    if inspection is None:
        with trio.fail_after(timeout):
            proc = await trio.run_process(
                ['wg', 'show', iface, field],
                capture_stdout=True,
                check=True,
            )
        inspection = proc.stdout.decode()

    if role == 'local':
        return addr.wg_pubkey == inspection.strip()
    return addr.wg_pubkey in inspection.split()
