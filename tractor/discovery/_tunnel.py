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

from collections.abc import (
    AsyncIterator,
    Sequence,
)
import base64
from contextlib import (
    AsyncExitStack,
    asynccontextmanager as acm,
)
import ipaddress
import sys
from typing import (
    Any,
    ClassVar,
    get_args,
    Literal,
    TYPE_CHECKING,
)

import msgspec
import multibase
import trio

from ..msg._local import ProcessLocal
from ._bindspace import (
    Bindspace,
    BindspaceRef,
    BindspaceSpec,
    open_bindspace,
)

if TYPE_CHECKING:
    from multiaddr import Multiaddr

    from ._addr import (
        Address,
        UnwrappedAddress,
    )
else:
    Address = Any
    Multiaddr = Any
    UnwrappedAddress = Any


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

    # the `multiaddr` proto name for this tunnel kind
    tunnel_key: ClassVar[str] = 'wg'


# the tunnel-spec union; grows as new tunnel kinds land
# (plain ip-in-udp, `veth`-in-netns, ..)
TunnelSpec = WGTunnelSpec


def mb_pubkey(
    wg8_key: str,
) -> str:
    '''
    Encode a `wg(8)` public key as multibase base64url.

    WireGuard public keys are exactly 32 bytes. Enforce that here
    before handing the `u`-prefixed result to `py-multiaddr`'s
    `/wg/` codec.

    '''
    raw: bytes = base64.b64decode(
        wg8_key,
        validate=True,
    )
    if (nbytes := len(raw)) != 32:
        raise ValueError(
            f'A `wg` public key must decode to 32 bytes, '
            f'not {nbytes}!'
        )

    return multibase.encode(
        'base64url',
        raw,
    ).decode('ascii')


def wg8_pubkey(
    mb_key: str,
) -> str:
    '''
    Decode a multibase public key to `wg(8)` standard base64.

    '''
    raw: bytes = multibase.decode(mb_key)
    if (nbytes := len(raw)) != 32:
        raise ValueError(
            f'A `wg` public key must decode to 32 bytes, '
            f'not {nbytes}!'
        )

    return base64.b64encode(raw).decode('ascii')


def _wg8_key_str(
    value: bytes|str,
) -> str:
    '''
    Validate and normalize one pyroute2-decoded WireGuard key.

    '''
    if isinstance(value, bytes):
        try:
            key: str = value.decode('ascii')
        except UnicodeDecodeError as exc:
            raise ValueError(
                'WireGuard key is not base64 ASCII!'
            ) from exc
    else:
        key = value

    # Reuse `mb_pubkey()`'s strict base64 + 32-byte validation.
    mb_pubkey(key)
    return key


class WGPeerConfig(
    ProcessLocal,
):
    '''
    Process-local configuration for one WireGuard peer.

    '''
    public_key: str
    allowed_ips: tuple[str, ...] = ()
    endpoint: tuple[str, int]|None = None
    preshared_key: str|None = None
    persistent_keepalive: int|None = None

    def __post_init__(self) -> None:
        '''
        Validate peer identity, routes, endpoint and secret policy.

        '''
        _wg8_key_str(self.public_key)
        if self.preshared_key is not None:
            _wg8_key_str(self.preshared_key)

        # Validate each route. `strict=False` accepts host bits;
        # pyroute2 will still receive each original declared string.
        allowed_ip: str
        for allowed_ip in self.allowed_ips:
            ipaddress.ip_network(
                allowed_ip,
                strict=False,
            )

        endpoint: tuple[str, int]|None = self.endpoint
        if endpoint is not None:
            host: str
            port: int
            host, port = endpoint
            ipaddress.ip_address(host)
            if (
                type(port) is not int
                or
                not 1 <= port <= 65535
            ):
                raise ValueError(
                    '`WGPeerConfig.endpoint` port must be in '
                    f'`1..65535`, not {port!r}!'
                )

        keepalive: int|None = self.persistent_keepalive
        if (
            keepalive is not None
            and
            (
                type(keepalive) is not int
                or
                not 0 <= keepalive <= 65535
            )
        ):
            raise ValueError(
                '`WGPeerConfig.persistent_keepalive` must be in '
                f'`0..65535` or `None`, not {keepalive!r}!'
            )

    def __repr__(self) -> str:
        '''
        Render public peer policy while redacting its preshared key.

        '''
        preshared: str|None = (
            '<redacted>'
            if self.preshared_key is not None
            else None
        )
        return (
            f'{type(self).__name__}('
            f'public_key={self.public_key!r}, '
            f'allowed_ips={self.allowed_ips!r}, '
            f'endpoint={self.endpoint!r}, '
            f'preshared_key={preshared!r}, '
            f'persistent_keepalive={self.persistent_keepalive!r})'
        )


class WGInterfaceConfig(
    ProcessLocal,
):
    '''
    Process-local secrets and routing inputs for one WireGuard iface.

    Public peer identity, endpoint and iface selection remain in
    `WGTunnelSpec`; private key material and local routing policy do
    not belong in an maddr-derived serializable declaration.

    '''
    private_key: str
    addresses: tuple[str, ...] = ()
    listen_port: int|None = None
    peers: tuple[WGPeerConfig, ...] = ()

    def __post_init__(self) -> None:
        '''
        Validate private identity, local CIDRs and peer uniqueness.

        '''
        _wg8_key_str(self.private_key)

        # Validate each local CIDR; no address is selected.
        address: str
        for address in self.addresses:
            ipaddress.ip_interface(address)

        listen_port: int|None = self.listen_port
        if (
            listen_port is not None
            and
            (
                type(listen_port) is not int
                or
                not 1 <= listen_port <= 65535
            )
        ):
            raise ValueError(
                '`WGInterfaceConfig.listen_port` must be in '
                f'`1..65535` or `None`, not {listen_port!r}!'
            )

        peer_keys: set[str] = set()
        peer: WGPeerConfig
        for peer in self.peers:
            if not isinstance(peer, WGPeerConfig):
                raise TypeError(
                    '`WGInterfaceConfig.peers` must contain '
                    '`WGPeerConfig` values!'
                )
            if peer.public_key in peer_keys:
                raise ValueError(
                    f'Duplicate WireGuard peer: {peer.public_key!r}'
                )
            peer_keys.add(peer.public_key)

    def __repr__(self) -> str:
        '''
        Render non-secret policy while redacting key material.

        '''
        return (
            f'{type(self).__name__}('
            f'private_key=<redacted>, '
            f'addresses={self.addresses!r}, '
            f'listen_port={self.listen_port!r}, '
            f'peers={self.peers!r})'
        )


WGRole = Literal['listen', 'dial']


def _wg_iface_settings(
    spec: WGTunnelSpec,
    config: WGInterfaceConfig,
    role: WGRole,
) -> tuple[int|None, tuple[dict[str, object], ...]]:
    '''
    Validate role policy and build pyroute2 WireGuard settings.

    '''
    if role not in get_args(WGRole):
        raise ValueError(
            f'Unsupported WireGuard role: {role!r}'
        )

    listen_port: int|None = config.listen_port
    bearer: tuple[str, int]|None = spec.bearer
    if (
        role == 'listen'
        and
        bearer is not None
    ):
        bearer_port: int = bearer[1]
        if (
            listen_port is not None
            and
            listen_port != bearer_port
        ):
            raise ValueError(
                f'`WGInterfaceConfig.listen_port={listen_port!r}` '
                f'conflicts with bearer port {bearer_port!r}!'
            )
        listen_port = bearer_port

    selected_peer: bool = False
    peer_settings: list[dict[str, object]] = []
    peer: WGPeerConfig
    for peer in config.peers:
        endpoint: tuple[str, int]|None = peer.endpoint
        if (
            role == 'dial'
            and
            peer.public_key == spec.peer_pubkey
        ):
            selected_peer = True
            if (
                endpoint is not None
                and
                bearer is not None
                and
                endpoint != bearer
            ):
                raise ValueError(
                    f'`WGPeerConfig.endpoint={endpoint!r}` conflicts '
                    f'with `WGTunnelSpec.bearer={bearer!r}`!'
                )
            endpoint = endpoint or bearer

        values: dict[str, object] = {
            'public_key': peer.public_key,
        }
        if peer.allowed_ips:
            values['allowed_ips'] = list(peer.allowed_ips)
            values['replace_allowed_ips'] = True
        if endpoint is not None:
            values['endpoint_addr'] = endpoint[0]
            values['endpoint_port'] = endpoint[1]
        if peer.preshared_key is not None:
            values['preshared_key'] = peer.preshared_key
        if peer.persistent_keepalive is not None:
            values['persistent_keepalive'] = (
                peer.persistent_keepalive
            )
        peer_settings.append(values)

    if (
        role == 'dial'
        and
        not selected_peer
    ):
        configured_keys: tuple[str, ...] = tuple(
            peer.public_key
            for peer in config.peers
        )
        raise ValueError(
            f'Dial target {spec.peer_pubkey!r} is not in '
            f'configured peer keys {configured_keys!r}!'
        )

    return listen_port, tuple(peer_settings)


def _sync_create_wg_iface(
    spec: WGTunnelSpec,
    config: WGInterfaceConfig,
    bindspace: Bindspace,
    listen_port: int|None,
    peers: tuple[dict[str, object], ...],
) -> None:
    '''
    Create and configure one WireGuard iface through pyroute2.

    '''
    try:
        from pyroute2 import (
            IPRoute,
            WireGuard,
        )
    except ImportError as exc:
        raise RuntimeError(
            'WireGuard provisioning requires the '
            '`tractor[wg]` extra.'
        ) from exc

    namespace_fd: int|None = bindspace.namespace_fd
    ipr: Any = IPRoute(
        netns=namespace_fd,
        flags=0,
    )
    created: bool = False
    try:
        ipr.link(
            'add',
            ifname=spec.iface,
            kind='wireguard',
        )
        created = True
        indices: list[int] = ipr.link_lookup(
            ifname=spec.iface,
        )
        if len(indices) != 1:
            raise RuntimeError(
                f'Expected one index for WG iface {spec.iface!r}, '
                f'got {indices!r}!'
            )
        index: int = indices[0]

        address: str
        for address in config.addresses:
            interface: (
                ipaddress.IPv4Interface
                | ipaddress.IPv6Interface
            ) = ipaddress.ip_interface(address)
            ipr.addr(
                'add',
                index=index,
                address=str(interface.ip),
                prefixlen=interface.network.prefixlen,
            )

        wg: Any = WireGuard(
            netns=namespace_fd,
            flags=0,
        )
        try:
            wg.set(
                spec.iface,
                private_key=config.private_key,
                listen_port=listen_port,
            )
            peer: dict[str, object]
            for peer in peers:
                wg.set(
                    spec.iface,
                    peer=peer,
                )
        finally:
            wg.close()

        ipr.link(
            'set',
            index=index,
            state='up',
        )
    except BaseException:
        if created:
            indices = ipr.link_lookup(
                ifname=spec.iface,
            )
            if indices:
                ipr.link(
                    'del',
                    index=indices[0],
                )
        raise
    finally:
        ipr.close()


def _sync_remove_wg_iface(
    spec: WGTunnelSpec,
    bindspace: Bindspace,
) -> None:
    '''
    Remove one owned WireGuard iface when it still exists.

    '''
    try:
        from pyroute2 import IPRoute
    except ImportError as exc:
        raise RuntimeError(
            'WireGuard teardown requires the `tractor[wg]` extra.'
        ) from exc

    ipr: Any = IPRoute(
        netns=bindspace.namespace_fd,
        flags=0,
    )
    try:
        indices: list[int] = ipr.link_lookup(
            ifname=spec.iface,
        )
        if indices:
            ipr.link(
                'del',
                index=indices[0],
            )
    finally:
        ipr.close()


@acm
async def open_wg_iface(
    spec: WGTunnelSpec,
    config: WGInterfaceConfig,
    bindspace: Bindspace,
    role: WGRole,
) -> AsyncIterator[WGTunnelSpec]:
    '''
    Create, configure and own one WireGuard interface.

    '''
    listen_port: int|None
    peers: tuple[dict[str, object], ...]
    listen_port, peers = _wg_iface_settings(
        spec,
        config,
        role,
    )
    created: bool = False
    try:
        with trio.CancelScope(shield=True):
            await trio.to_thread.run_sync(
                _sync_create_wg_iface,
                spec,
                config,
                bindspace,
                listen_port,
                peers,
                abandon_on_cancel=False,
            )
            created = True
        yield spec
    finally:
        if created:
            with trio.CancelScope(shield=True):
                await trio.to_thread.run_sync(
                    _sync_remove_wg_iface,
                    spec,
                    bindspace,
                    abandon_on_cancel=False,
                )


@acm
async def open_wg_bindspace(
    bindspace_spec: BindspaceSpec,
    layers: Sequence[tuple[WGTunnelSpec, WGInterfaceConfig]],
    role: WGRole,
) -> AsyncIterator[Bindspace]:
    '''
    Open one bindspace and its ordered WireGuard interface stack.

    `layers` is an interface stack declared outermost first:

        application scope
               |
        layers[-1]         <- last entered, first exited
               |
              ...
               |
        layers[0]          <- first entered, last exited
               |
        bindspace

    `AsyncExitStack` builds it bottom-up in declaration order and
    unwinds it top-down before the bindspace closes.

    '''
    layer_stack: tuple[
        tuple[WGTunnelSpec, WGInterfaceConfig],
        ...,
    ] = tuple(layers)
    async with AsyncExitStack() as stack:
        bindspace: Bindspace = await (
            stack.enter_async_context(
                open_bindspace(bindspace_spec)
            )
        )

        layer: tuple[WGTunnelSpec, WGInterfaceConfig]
        for layer in layer_stack:
            tunnel_spec: WGTunnelSpec
            config: WGInterfaceConfig
            tunnel_spec, config = layer
            await stack.enter_async_context(
                open_wg_iface(
                    tunnel_spec,
                    config,
                    bindspace,
                    role,
                )
            )

        yield bindspace


def _sync_read_wg_keys(
    iface: str,
    netns: str|None,
) -> tuple[str, tuple[str, ...]]:
    '''
    Read one WireGuard device using pyroute2's synchronous API.

    This whole function runs in a worker thread because pyroute2's
    synchronous netlink API owns a private asyncio loop.

    '''
    if sys.platform != 'linux':
        raise NotImplementedError(
            'WireGuard netlink inspection is Linux-only!'
        )

    try:
        from pyroute2 import WireGuard
    except ImportError as exc:
        raise RuntimeError(
            'WireGuard inspection requires the `tractor[wg]` extra.'
        ) from exc

    # Pyroute2 defaults namespace flags to `os.O_CREAT`; a read must
    # never create a missing namespace as a side effect.
    wg: Any = WireGuard(
        netns=netns,
        flags=0,
    )
    try:
        infos: tuple[Any, ...] = tuple(wg.info(iface))
    finally:
        wg.close()

    pubkey: str|None = None
    peers: list[str] = []
    info: Any
    for info in infos:
        raw_pubkey: Any
        if raw_pubkey := info.get_attr(
            'WGDEVICE_A_PUBLIC_KEY'
        ):
            next_pubkey: str = _wg8_key_str(raw_pubkey)
            if (
                pubkey is not None
                and
                pubkey != next_pubkey
            ):
                raise RuntimeError(
                    f'Conflicting public keys returned for '
                    f'{iface!r}!'
                )
            pubkey = next_pubkey

        peer: Any
        for peer in (
            info.get_attr('WGDEVICE_A_PEERS')
            or ()
        ):
            raw_peer: Any
            if raw_peer := peer.get_attr(
                'WGPEER_A_PUBLIC_KEY'
            ):
                peers.append(_wg8_key_str(raw_peer))

    if pubkey is None:
        raise RuntimeError(
            f'No public key returned for WireGuard iface '
            f'{iface!r}!'
        )

    return (
        pubkey,
        tuple(dict.fromkeys(peers)),
    )


async def _read_wg_keys(
    iface: str,
    netns: str|None,
) -> tuple[str, tuple[str, ...]]:
    '''
    Read one WireGuard key snapshot without blocking Trio.

    '''
    return await trio.to_thread.run_sync(
        _sync_read_wg_keys,
        iface,
        netns,
        abandon_on_cancel=False,
    )


async def read_wg_pubkey(
    iface: str = 'wg0',
    netns: str|None = None,
) -> str:
    '''
    Read a WireGuard interface's public key through netlink.

    '''
    keys: tuple[
        str,
        tuple[str, ...],
    ] = await _read_wg_keys(
        iface,
        netns,
    )
    return keys[0]


async def read_wg_peers(
    iface: str = 'wg0',
    netns: str|None = None,
) -> tuple[str, ...]:
    '''
    Read configured peer public keys through netlink.

    '''
    keys: tuple[
        str,
        tuple[str, ...],
    ] = await _read_wg_keys(
        iface,
        netns,
    )
    return keys[1]


async def verify_wg_peer(
    spec: WGTunnelSpec,
) -> bool:
    '''
    Verify a declared WireGuard identity against local kernel state.

    A source/listen maddr names the local interface key, while a
    destination/dial maddr names one configured peer. Accept either
    match without making verification an implicit part of parsing.

    '''
    declared_key: str = _wg8_key_str(spec.peer_pubkey)
    keys: tuple[
        str,
        tuple[str, ...],
    ] = await _read_wg_keys(
        spec.iface,
        spec.netns,
    )
    return (
        declared_key == keys[0]
        or
        declared_key in keys[1]
    )


def _wg_proto_code() -> int:
    '''
    Deliver the installed `py-multiaddr` `/wg/` protocol code.

    `wg` support is merged upstream but not yet in a release, so
    fail clearly when tractor was installed without the pinned rev.

    '''
    from multiaddr.exceptions import ProtocolNotFoundError
    from multiaddr.protocols import protocol_with_name

    try:
        return protocol_with_name('wg').code
    except ProtocolNotFoundError as exc:
        raise RuntimeError(
            'Installed `py-multiaddr` has no `/wg/` protocol!\n'
            'Install py-multiaddr#108 or use tractor\'s pinned '
            'dependency revision.\n'
        ) from exc


class TunnelledAddress(
    msgspec.Struct,
    frozen=True,
    omit_defaults=True,
):
    '''
    An `Address` annotated with the tunnel it must be reached
    *through*.

    Address-level properties delegate to `.overlay`, so proto-key
    guards and `.unwrap()` retain their existing meaning and
    **nothing new crosses the wire**. Transport boundaries which
    dispatch on exact type or declaring module must first call
    `strip_tunnels()`.

    '''
    overlay: Address|TunnelledAddress
    tunnel: TunnelSpec
    bindspace_ref: BindspaceRef|None = None

    def __post_init__(self) -> None:
        '''
        Validate the retained ref against the tunnel declaration.

        '''
        ref: BindspaceRef|None = self.bindspace_ref
        if ref is None:
            return
        if not isinstance(ref, BindspaceRef):
            raise TypeError(
                '`TunnelledAddress.bindspace_ref` must be a '
                '`BindspaceRef` or `None`!'
            )

        declared_netns: str|None = self.tunnel.netns
        if (
            declared_netns is not None
            and
            ref.key != declared_netns
        ):
            raise ValueError(
                f'Declared netns {declared_netns!r} does not match '
                f'realized bindspace key {ref.key!r}!'
            )

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
        Return the realized ref or declared tunnel netns.

        '''
        ref: BindspaceRef|None = self.bindspace_ref
        if ref is not None:
            return (
                ref.kind,
                ref.inode,
            )

        if (netns := self.tunnel.netns) is None:
            return self.overlay.namespace

        return ('netns', netns)

    def with_bindspace_ref(
        self,
        ref: BindspaceRef,
    ) -> TunnelledAddress:
        '''
        Return a copy retaining one realized bindspace ref.

        '''
        realized: TunnelledAddress = msgspec.structs.replace(
            self,
            bindspace_ref=ref,
        )
        return realized

    def __repr__(self) -> str:
        return (
            f'{type(self).__name__}(\n'
            f'  overlay={self.overlay!r},\n'
            f'  via={self.tunnel.tunnel_key!r} '
            f'iface={self.tunnel.iface!r},\n'
            f')'
        )


def _wg_bearer(
    bearer_ma: Multiaddr,
    source_ma: Multiaddr,
) -> tuple[str, int]:
    '''
    Parse one kernel-owned `wg` bearer endpoint.

    '''
    proto_names: list[str] = [
        proto.name
        for proto in bearer_ma.protocols()
    ]
    match proto_names:
        case [('ip4' | 'ip6') as ip_proto, 'udp']:
            return (
                bearer_ma.value_for_protocol(ip_proto),
                int(bearer_ma.value_for_protocol('udp')),
            )

        case _:
            raise ValueError(
                f'Bad `wg` bearer, expected '
                f'`/ip4|ip6/<host>/udp/<port>`\n'
                f'got: {bearer_ma}\n'
                f'from maddr: {source_ma}\n'
            )


def parse_wg_maddr(
    maddr: str|Multiaddr,
) -> TunnelledAddress:
    '''
    Parse a `wg` maddr stack into nested tunnel annotations.

    Pure: every segment operation delegates to `py-multiaddr`.
    Repeated `.decapsulate_code()` calls peel the last `/wg/`
    first, while `.split()` and `.join()` isolate that tunnel's
    bearer without parsing slash-delimited strings ourselves.

    '''
    from multiaddr import Multiaddr

    ma: Multiaddr = (
        maddr
        if isinstance(maddr, Multiaddr)
        else Multiaddr(maddr)
    )
    wg_code: int = _wg_proto_code()
    segs: list[Multiaddr] = ma.split()
    proto_names: list[str] = [
        proto.name
        for seg in segs
        for proto in seg.protocols()
    ]
    if 'wg' not in proto_names:
        raise ValueError(
            f'Not a `wg`-tunnelled maddr; no `/wg/` segment!\n'
            f'maddr: {ma}\n'
        )

    final_wg_i: int = len(proto_names) - 1
    final_wg_i -= proto_names[::-1].index('wg')
    overlay_ma: Multiaddr = Multiaddr.join(
        *segs[final_wg_i + 1:]
    )
    overlay_names: list[str] = [
        proto.name
        for proto in overlay_ma.protocols()
    ]
    match overlay_names:
        case [('ip4' | 'ip6'), 'tcp']:
            from ._multiaddr import parse_maddr
            overlay: Address|TunnelledAddress = parse_maddr(
                str(overlay_ma)
            )

        case []:
            raise ValueError(
                f'`wg` maddr declares no overlay endpoint!\n'
                f'Append the endpoint tractor should bind.\n'
                f'maddr: {ma}\n'
            )

        case _:
            raise ValueError(
                f'Unsupported `wg` overlay protocol combo: '
                f'{overlay_names!r}\n'
                f'overlay: {overlay_ma}\n'
                f'from maddr: {ma}\n'
            )

    cursor: Multiaddr = ma
    while any(
        proto.name == 'wg'
        for proto in cursor.protocols()
    ):
        cursor_segs: list[Multiaddr] = cursor.split()
        cursor_names: list[str] = [
            proto.name
            for seg in cursor_segs
            for proto in seg.protocols()
        ]
        wg_i: int = len(cursor_names) - 1
        wg_i -= cursor_names[::-1].index('wg')
        mb_key: str = cursor_segs[wg_i].value_for_protocol('wg')

        bearer_prefix: Multiaddr = cursor.decapsulate_code(
            wg_code
        )
        prefix_segs: list[Multiaddr] = bearer_prefix.split()
        prefix_names: list[str] = [
            proto.name
            for seg in prefix_segs
            for proto in seg.protocols()
        ]
        prior_wg_i: int = (
            len(prefix_names) - 1
            - prefix_names[::-1].index('wg')
            if 'wg' in prefix_names
            else -1
        )
        bearer_ma: Multiaddr = Multiaddr.join(
            *prefix_segs[prior_wg_i + 1:]
        )
        overlay = TunnelledAddress(
            overlay=overlay,
            tunnel=WGTunnelSpec(
                peer_pubkey=wg8_pubkey(mb_key),
                bearer=_wg_bearer(bearer_ma, ma),
            ),
        )
        cursor = bearer_prefix

    return overlay


def mk_wg_maddr(
    addr: TunnelledAddress,
) -> Multiaddr:
    '''
    Compose nested tunnel annotations as a canonical `wg` maddr.

    Only the peer key and bearer have maddr representations. Local
    interface, namespace, and allowed-IP config remains local.

    '''
    from multiaddr import Multiaddr

    _wg_proto_code()
    if (bearer := addr.tunnel.bearer) is None:
        raise ValueError(
            f'Can not compose a `wg` maddr without a bearer!\n'
            f'tunnel: {addr.tunnel!r}\n'
        )

    bindable: Address = strip_tunnels(addr)
    if bindable.proto_key != 'tcp':
        raise ValueError(
            f'Unsupported `wg` overlay proto-key: '
            f'{bindable.proto_key!r}\n'
            f'overlay: {bindable!r}\n'
        )

    host, port = bearer
    ip = ipaddress.ip_address(host)
    ip_proto: str = (
        'ip4'
        if ip.version == 4
        else 'ip6'
    )
    bearer_ma = Multiaddr(
        f'/{ip_proto}/{host}/udp/{port}'
    )
    key_ma = Multiaddr(
        f'/wg/{mb_pubkey(addr.tunnel.peer_pubkey)}'
    )

    from ._multiaddr import mk_maddr
    overlay_ma: Multiaddr = mk_maddr(addr.overlay)
    return (
        bearer_ma
        .encapsulate(key_ma)
        .encapsulate(overlay_ma)
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
