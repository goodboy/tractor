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

"""
Discovery (protocols) API for automatic addressing and location
management of (service) actors.

"""
from __future__ import annotations
import ipaddress
import socket
from typing import (
    AsyncGenerator,
    AsyncContextManager,
    TYPE_CHECKING,
)
from contextlib import asynccontextmanager as acm

from tractor.log import get_logger
from ..trionics import (
    gather_contexts,
    collapse_eg,
)
from ..ipc import _connect_chan, Channel
from ..ipc._tcp import TCPAddress
from ..ipc._uds import UDSAddress
from ._addr import (
    UnwrappedAddress,
    Address,
    wrap_address,
)
from ..runtime._portal import (
    Portal,
    open_portal,
    LocalPortal,
)
from ..runtime._state import (
    current_actor,
    _runtime_vars,
    _def_tpt_proto,
)

if TYPE_CHECKING:
    from ..runtime._runtime import Actor


log = get_logger()


def _is_local_addr(addr: Address) -> bool:
    '''
    Determine whether `addr` is reachable on the
    local host by inspecting address type and
    comparing hostnames/PIDs.

    - `UDSAddress` is always local (filesystem-bound)
    - `TCPAddress` is local when its host is a
      loopback IP or matches one of the machine's
      own interface addresses.

    '''
    if isinstance(addr, UDSAddress):
        return True

    if isinstance(addr, TCPAddress):
        try:
            ip = ipaddress.ip_address(addr._host)
        except ValueError:
            return False

        if ip.is_loopback:
            return True

        # check if this IP belongs to any of our
        # local network interfaces.
        try:
            local_ips: set[str] = {
                info[4][0]
                for info in socket.getaddrinfo(
                    socket.gethostname(),
                    None,
                )
            }
            return addr._host in local_ips
        except socket.gaierror:
            return False

    return False


def prefer_addr(
    addrs: list[UnwrappedAddress],
) -> UnwrappedAddress:
    '''
    Select the "best" transport address from a
    multihomed actor's address list based on
    locality heuristics.

    Preference order (highest -> lowest):
    1. UDS (same-host guaranteed, lowest overhead)
    2. TCP loopback / same-host IP
    3. TCP remote (only option for distributed)

    When multiple addrs share the same priority
    tier, the last-registered (latest) entry is
    preferred.

    '''
    if len(addrs) == 1:
        return addrs[0]

    local_uds: list[UnwrappedAddress] = []
    local_tcp: list[UnwrappedAddress] = []
    remote: list[UnwrappedAddress] = []

    for unwrapped in addrs:
        wrapped: Address = wrap_address(unwrapped)
        if isinstance(wrapped, UDSAddress):
            local_uds.append(unwrapped)
        elif _is_local_addr(wrapped):
            local_tcp.append(unwrapped)
        else:
            remote.append(unwrapped)

    # prefer UDS > local TCP > remote TCP;
    # within each tier take the latest entry.
    if local_uds:
        return local_uds[-1]
    if local_tcp:
        return local_tcp[-1]
    if remote:
        return remote[-1]

    # fallback: last registered addr
    return addrs[-1]


@acm
async def get_registry(
    addr: UnwrappedAddress|None = None,
) -> AsyncGenerator[
    Portal|LocalPortal|None,
    None,
]:
    '''
    Return a portal instance connected to a local or remote
    registry-service actor; if a connection already exists re-use it
    (presumably to call a `.register_actor()` registry runtime RPC
    ep).

    '''
    actor: Actor = current_actor()
    if actor.is_registrar:
        # we're already the registrar
        # (likely a re-entrant call from the registrar actor)
        yield LocalPortal(
            actor,
            Channel(transport=None)
            # ^XXX, we DO NOT actually provide nor connect an
            # underlying transport since this is merely an API shim.
        )
    else:
        # TODO: try to look pre-existing connection from
        # `Server._peers` and use it instead?
        async with (
            _connect_chan(addr) as chan,
            open_portal(chan) as regstr_ptl,
        ):
            yield regstr_ptl


@acm
async def get_root(**kwargs) -> AsyncGenerator[Portal, None]:
    '''
    Deliver the current actor's "root process" actor (yes in actor
    and proc tree terms) by delivering a `Portal` from the spawn-time
    provided contact address.

    '''
    # TODO: rename mailbox to `_root_maddr` when we finally
    # add and impl libp2p multi-addrs?
    addr = _runtime_vars['_root_mailbox']

    async with (
        _connect_chan(addr) as chan,
        open_portal(chan, **kwargs) as portal,
    ):
        yield portal


def get_peer_by_name(
    name: str,
    # uuid: str|None = None,

) -> list[Channel]|None:  # at least 1
    '''
    Scan for an existing connection (set) to a named actor
    and return any channels from `Server._peers: dict`.

    This is an optimization method over querying the registrar for
    the same info.

    '''
    actor: Actor = current_actor()
    to_scan: dict[tuple, list[Channel]] = actor.ipc_server._peers.copy()

    # TODO: is this ever needed? creates a duplicate channel on actor._peers
    # when multiple find_actor calls are made to same actor from a single ctx
    # which causes actor exit to hang waiting forever on
    # `actor._no_more_peers.wait()` in `_runtime.async_main`

    # pchan: Channel|None = actor._parent_chan
    # if pchan and pchan.uid not in to_scan:
    #     to_scan[pchan.uid].append(pchan)

    for aid, chans in to_scan.items():
        _, peer_name = aid
        if name == peer_name:
            if not chans:
                log.warning(
                    'No IPC chans for matching peer {peer_name}\n'
                )
                continue
            return chans

    return None


@acm
async def query_actor(
    name: str,
    regaddr: UnwrappedAddress|None = None,

) -> AsyncGenerator[
    tuple[UnwrappedAddress|None, Portal|LocalPortal|None],
    None,
]:
    '''
    Lookup a transport address (by actor name) via querying a registrar
    listening @ `regaddr`.

    Yields a `tuple` of `(addr, reg_portal)` where,
    - `addr` is the transport protocol (socket) address or `None` if
      no entry under that name exists,
    - `reg_portal` is the `Portal` (or `LocalPortal` when the
      current actor is the registrar) used for the lookup (or
      `None` when the peer was found locally via
      `get_peer_by_name()`).

    '''
    actor: Actor = current_actor()
    if (
        name == 'registrar'
        and
        actor.is_registrar
    ):
        raise RuntimeError(
            'The current actor IS the registry!?'
        )

    maybe_peers: list[Channel]|None = get_peer_by_name(name)
    if maybe_peers:
        yield maybe_peers[0].raddr, None
        return

    reg_portal: Portal|LocalPortal
    regaddr: Address = wrap_address(regaddr) or actor.reg_addrs[0]
    async with get_registry(regaddr) as reg_portal:
        addrs: list[UnwrappedAddress]|None = (
            await reg_portal.run_from_ns(
                'self',
                'find_actor',
                name=name,
            )
        )
        if addrs:
            addr: UnwrappedAddress = prefer_addr(addrs)
        else:
            addr = None
        yield addr, reg_portal

@acm
async def maybe_open_portal(
    addr: UnwrappedAddress,
    name: str,
):
    '''
    Open a `Portal` to the actor serving @ `addr` or `None` if no
    peer can be contacted or found.

    '''
    async with query_actor(
        name=name,
        regaddr=addr,
    ) as (addr, reg_portal):
        if not addr:
            yield None
            return

        try:
            async with _connect_chan(addr) as chan:
                async with open_portal(chan) as portal:
                    yield portal

        # most likely we were unable to connect the
        # transport and there is likely a stale entry in
        # the registry actor's table, thus we need to
        # instruct it to clear that stale entry and then
        # more silently (pretend there was no reason but
        # to) indicate that the target actor can't be
        # contacted at that addr.
        except OSError:
            # NOTE: ensure we delete the stale entry
            # from the registrar actor when available.
            if reg_portal is not None:
                uid: tuple[str, str]|None = await reg_portal.run_from_ns(
                    'self',
                    'delete_addr',
                    addr=addr,
                )
                if uid:
                    log.warning(
                        f'Deleted stale registry entry !\n'
                        f'addr: {addr!r}\n'
                        f'uid: {uid!r}\n'
                    )
                else:
                    log.warning(
                        f'No registry entry found for addr: {addr!r}'
                    )
            else:
                log.warning(
                    f'Connection to {addr!r} failed'
                    f' and no registry portal available'
                    f' to delete stale entry.'
                )
            yield None


@acm
async def find_actor(
    name: str,
    registry_addrs: list[UnwrappedAddress]|None = None,
    enable_transports: list[str] = [_def_tpt_proto],

    only_first: bool = True,
    raise_on_none: bool = False,

) -> AsyncGenerator[
    Portal | list[Portal] | None,
    None,
]:
    '''
    Ask the registrar to find actor(s) by name.

    Returns a connected portal to the last registered
    matching actor known to the registrar.

    '''
    # optimization path, use any pre-existing peer channel
    maybe_peers: list[Channel]|None = get_peer_by_name(name)
    if maybe_peers and only_first:
        async with open_portal(maybe_peers[0]) as peer_portal:
            yield peer_portal
            return

    if not registry_addrs:
        # XXX NOTE: make sure to dynamically read the value on
        # every call since something may change it globally (eg.
        # like in our discovery test suite)!
        from ._addr import default_lo_addrs
        registry_addrs = (
            _runtime_vars['_registry_addrs']
            or
            default_lo_addrs(enable_transports)
        )

    maybe_portals: list[
        AsyncContextManager[UnwrappedAddress]
    ] = list(
        maybe_open_portal(
            addr=addr,
            name=name,
        )
        for addr in registry_addrs
    )
    portals: list[Portal]
    async with (
        collapse_eg(),
        gather_contexts(
            mngrs=maybe_portals,
        ) as portals,
    ):
        # log.runtime(
        #     'Gathered portals:\n'
        #     f'{portals}'
        # )
        # NOTE: `gather_contexts()` will return a
        # `tuple[None, None, ..., None]` if no contact
        # can be made with any regstrar at any of the
        # N provided addrs!
        if not any(portals):
            if raise_on_none:
                raise RuntimeError(
                    f'No actor {name!r} found registered @ {registry_addrs!r}'
                )
            yield None
            return

        portals: list[Portal] = list(portals)
        if only_first:
            yield portals[0]

        else:
            # TODO: currently this may return multiple portals
            # given there are multi-homed or multiple registrars..
            # SO, we probably need de-duplication logic?
            yield portals


@acm
async def wait_for_actor(
    name: str,
    registry_addr: UnwrappedAddress | None = None,

) -> AsyncGenerator[Portal, None]:
    '''
    Wait on at least one peer actor to register `name` with the
    registrar, yield a `Portal to the first registree.

    '''
    actor: Actor = current_actor()

    # optimization path, use any pre-existing peer channel
    maybe_peers: list[Channel]|None = get_peer_by_name(name)
    if maybe_peers:
        async with open_portal(maybe_peers[0]) as peer_portal:
            yield peer_portal
            return

    regaddr: UnwrappedAddress = (
        registry_addr
        or
        actor.reg_addrs[0]
    )
    # TODO: use `.trionics.gather_contexts()` like
    # above in `find_actor()` as well?
    reg_portal: Portal
    async with get_registry(regaddr) as reg_portal:
        addrs = await reg_portal.run_from_ns(
            'self',
            'wait_for_actor',
            name=name,
        )

        # select the best transport addr from
        # the (possibly multihomed) addr list.
        addr: UnwrappedAddress = prefer_addr(addrs)

        async with _connect_chan(addr) as chan:
            async with open_portal(chan) as portal:
                yield portal
