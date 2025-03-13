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
from typing import (
    AsyncGenerator,
    AsyncContextManager,
    TYPE_CHECKING,
)
from contextlib import asynccontextmanager as acm

from tractor.log import get_logger
from .trionics import gather_contexts
from .ipc import _connect_chan, Channel
from ._portal import (
    Portal,
    open_portal,
    LocalPortal,
)
from ._state import (
    current_actor,
    _runtime_vars,
)

if TYPE_CHECKING:
    from ._runtime import Actor


log = get_logger(__name__)


@acm
async def get_registry(
    host: str,
    port: int,

) -> AsyncGenerator[
    Portal | LocalPortal | None,
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
        # we're already the arbiter
        # (likely a re-entrant call from the arbiter actor)
        yield LocalPortal(
            actor,
            Channel((host, port))
        )
    else:
        # TODO: try to look pre-existing connection from
        # `Actor._peers` and use it instead?
        async with (
            _connect_chan(host, port) as chan,
            open_portal(chan) as regstr_ptl,
        ):
            yield regstr_ptl



@acm
async def get_root(
    **kwargs,
) -> AsyncGenerator[Portal, None]:

    # TODO: rename mailbox to `_root_maddr` when we finally
    # add and impl libp2p multi-addrs?
    host, port = _runtime_vars['_root_mailbox']
    assert host is not None

    async with (
        _connect_chan(host, port) as chan,
        open_portal(chan, **kwargs) as portal,
    ):
        yield portal


def get_peer_by_name(
    name: str,
    # uuid: str|None = None,

) -> list[Channel]|None:  # at least 1
    '''
    Scan for an existing connection (set) to a named actor
    and return any channels from `Actor._peers`.

    This is an optimization method over querying the registrar for
    the same info.

    '''
    actor: Actor = current_actor()
    to_scan: dict[tuple, list[Channel]] = actor._peers.copy()
    pchan: Channel|None = actor._parent_chan
    if pchan:
        to_scan[pchan.uid].append(pchan)

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
    regaddr: tuple[str, int]|None = None,

) -> AsyncGenerator[
    tuple[str, int]|None,
    None,
]:
    '''
    Lookup a transport address (by actor name) via querying a registrar
    listening @ `regaddr`.

    Returns the transport protocol (socket) address or `None` if no
    entry under that name exists.

    '''
    actor: Actor = current_actor()
    if (
        name == 'registrar'
        and actor.is_registrar
    ):
        raise RuntimeError(
            'The current actor IS the registry!?'
        )

    maybe_peers: list[Channel]|None = get_peer_by_name(name)
    if maybe_peers:
        yield maybe_peers[0].raddr
        return

    reg_portal: Portal
    regaddr: tuple[str, int] = regaddr or actor.reg_addrs[0]
    async with get_registry(*regaddr) as reg_portal:
        # TODO: return portals to all available actors - for now
        # just the last one that registered
        sockaddr: tuple[str, int] = await reg_portal.run_from_ns(
            'self',
            'find_actor',
            name=name,
        )
        yield sockaddr


@acm
async def maybe_open_portal(
    addr: tuple[str, int],
    name: str,
):
    async with query_actor(
        name=name,
        regaddr=addr,
    ) as sockaddr:
        pass

    if sockaddr:
        async with _connect_chan(*sockaddr) as chan:
            async with open_portal(chan) as portal:
                yield portal
    else:
        yield None


@acm
async def find_actor(
    name: str,
    registry_addrs: list[tuple[str, int]]|None = None,

    only_first: bool = True,
    raise_on_none: bool = False,

) -> AsyncGenerator[
    Portal | list[Portal] | None,
    None,
]:
    '''
    Ask the arbiter to find actor(s) by name.

    Returns a connected portal to the last registered matching actor
    known to the arbiter.

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
        from . import _root
        registry_addrs = (
            _runtime_vars['_registry_addrs']
            or
            _root._default_lo_addrs
        )

    maybe_portals: list[
        AsyncContextManager[tuple[str, int]]
    ] = list(
        maybe_open_portal(
            addr=addr,
            name=name,
        )
        for addr in registry_addrs
    )
    portals: list[Portal]
    async with gather_contexts(
        mngrs=maybe_portals,
    ) as portals:
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
                    f'No actor "{name}" found registered @ {registry_addrs}'
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
    registry_addr: tuple[str, int] | None = None,

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

    regaddr: tuple[str, int] = (
        registry_addr
        or
        actor.reg_addrs[0]
    )
    # TODO: use `.trionics.gather_contexts()` like
    # above in `find_actor()` as well?
    reg_portal: Portal
    async with get_registry(*regaddr) as reg_portal:
        sockaddrs = await reg_portal.run_from_ns(
            'self',
            'wait_for_actor',
            name=name,
        )

        # get latest registered addr by default?
        # TODO: offer multi-portal yields in multi-homed case?
        sockaddr: tuple[str, int] = sockaddrs[-1]

        async with _connect_chan(*sockaddr) as chan:
            async with open_portal(chan) as portal:
                yield portal
