# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
r'''
Tunnelled addresses: an `Address` that rides *inside* a tunnel.

A tunnel (`wg`, and later plain ip-in-udp, `veth`-in-netns, ..) is
**not** a `MsgTransport`. Its data plane is transparent to the
application's `socket(2)`, so it never gets its own entry in
`._addr._address_types` nor a `MsgpackTransport` impl. Instead it
*annotates* an existing L4 addr, and this module carries that
annotation beside it.

That does not mean tractor can never provision the tunnel. Layer A
assumes an externally configured iface; a later bindspace lifecycle
may create its iface, netns, routes, and kernel-owned UDP listener
through netlink/`pyroute2`. The distinction is that this
control-plane work does not turn the bearer into an application
`Endpoint`.

Naming follows `py-multiaddr`'s encapsulation model, where earlier
maddr segs wrap later ones (`.encapsulate()` appends):

    /ip4/192.168.1.50/udp/51820/wg/u<key>/ip4/10.0.11.1/tcp/1616
    \_______ bearer __________/\__ key __/\______ overlay ______/

- **bearer**: the underlay ep the tunnel iface listens on
  (`wg(8)`'s `ListenPort`). The kernel owns this data-plane socket;
  tractor may later provision it through a bindspace lifecycle but
  never treats it as a `MsgTransport` listener.
- **overlay**: the ep `tractor` actually binds/dials, i.e. the
  application IPC endpoint handled by `Endpoint`/`MsgTransport`.

We avoid `inner`/`outer` deliberately: in a *call* stack "inner"
reads as higher-up and later-called, whereas here the
encapsulated addr is bound *first* and sits deeper in the maddr.

XXX XXX READ THIS BEFORE USING XXX XXX
--------------------------------------
A `TunnelledAddress` **must be unwrapped to `.overlay` before it
reaches `Endpoint`**. `Endpoint.start_listener()` resolves its
listener fns by `inspect.getmodule(self.addr)`, so a wrapper
would resolve to *this* module rather than the transport's and
silently fail to find `start_listener()`.

If a wrapper reaches `Endpoint`, its backend lookup resolves this
module instead of the overlay transport module:

    tpt_mod = inspect.getmodule(self.addr)
    await tpt_mod.start_listener(addr=self.addr)

This module intentionally does not impersonate that transport API.
Unwrap at the parse or bindspace boundary; see `.overlay` and
`strip_tunnels()`.

'''
from __future__ import annotations
from typing import (
    ClassVar,
    TYPE_CHECKING,
)

import msgspec

if TYPE_CHECKING:
    from ._addr import (
        Address,
        UnwrappedAddress,
    )


class WGTunnelSpec(
    msgspec.Struct,
    frozen=True,
):
    '''
    The `wg`-specific half of a tunnel annotation.

    Everything here is an *interface-layer* concern owned by
    `wg(8)`/the kernel. A later tractor bindspace lifecycle may
    provision it through netlink, but it is never an application
    `MsgTransport` endpoint.

    '''
    # tunnel peer pubkey in the std-base64 `wg(8)` form, i.e.
    # directly comparable to `wg show <if> peers` output
    peer_pubkey: str

    # the underlay `(ip, udp-port)` the wg iface listens on, i.e.
    # wg's `ListenPort`. The kernel owns the socket even when a
    # tractor bindspace lifecycle provisions it. `None` when the
    # maddr declared only a key (identity) and the bearer is
    # implied by local cfg.
    bearer: tuple[str, int]|None = None

    iface: str = 'wg0'
    netns: str|None = None

    # layer-C-only fields, unset in layer A
    maybe_allowed_ips: tuple[str, ...] = ()

    # the `multiaddr` proto name for this tunnel kind
    tunnel_key: ClassVar[str] = 'wg'


# the tunnel-spec union; grows as new tunnel kinds land
# (plain ip-in-udp, `veth`-in-netns, ..)
TunnelSpec = WGTunnelSpec


class TunnelledAddress(
    msgspec.Struct,
    frozen=True,
):
    '''
    An `Address` annotated with the tunnel it must be reached
    *through*.

    Everything addressy delegates to `.overlay`, so every
    existing table lookup (`_addr_to_transport`, the
    `enable_transports` guard, `transport_from_addr()`) keeps
    working untouched, and `.unwrap()` delegating means **nothing
    new crosses the wire**.

    '''
    overlay: Address
    tunnel: TunnelSpec

    # ---- delegated, so the runtime can't tell the difference ----

    @property
    def proto_key(self) -> str:
        '''
        The *overlay's* proto-key — a tunnel has no transport of
        its own.

        NOTE, this is a property whereas `Address.proto_key` is
        spec'd as a `ClassVar`. That's deliberate: the value is
        only knowable per-instance here, and this type is never
        registered in `_address_types`, so no class-level access
        of it should ever occur.

        '''
        return self.overlay.proto_key

    @property
    def is_valid(self) -> bool:
        return self.overlay.is_valid

    @property
    def bindspace(self) -> str:
        return self.overlay.bindspace

    def unwrap(self) -> UnwrappedAddress:
        '''
        Delegate to `.overlay`, so the tunnel annotation is
        **not** serialized and no peer needs to understand it.

        '''
        return self.overlay.unwrap()

    # ---- the tunnel's own contribution ----

    @property
    def namespace(self) -> tuple[str, str|int]|None:
        '''
        The tunnel's netns, when it declares one.

        This is the first real consumer of `Address.namespace`,
        spec'd in the `Address` protocol since day one and
        implemented by no backend.

        XXX NOTE, "implemented by no backend" is literal: neither
        `TCPAddress` nor `UDSAddress` defines `.namespace` at all,
        so a plain attr access on an overlay raises
        `AttributeError` rather than yielding `None`. Hence the
        `getattr()` — drop it once the backends actually declare
        the member.

        '''
        if (netns := self.tunnel.netns) is None:
            return getattr(self.overlay, 'namespace', None)

        return ('netns', netns)

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}(\n'
            f'  overlay={self.overlay!r},\n'
            f'  via={self.tunnel.tunnel_key!r} '
            f'iface={self.tunnel.iface!r},\n'
            f')'
        )


def strip_tunnels(
    addr: Address|TunnelledAddress,
) -> Address:
    '''
    Deliver the bindable `Address`, peeling any tunnel
    annotation(s).

    Pure. Idempotent on an un-tunnelled `Address`, and loops so
    a nested (tunnel-in-tunnel) stack collapses in one call.

    Call this at every bind/dial boundary.

    '''
    while isinstance(addr, TunnelledAddress):
        addr = addr.overlay

    return addr


def tunnels_of(
    addr: Address|TunnelledAddress,
) -> tuple[TunnelSpec, ...]:
    '''
    Deliver every tunnel spec wrapping `addr`, outermost first.

    Pure; empty for an un-tunnelled `Address`.

    '''
    specs: list[TunnelSpec] = []
    while isinstance(addr, TunnelledAddress):
        specs.append(addr.tunnel)
        addr = addr.overlay

    return tuple(specs)
