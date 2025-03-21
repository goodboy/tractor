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
import warnings

from .trionics import gather_contexts
from ._ipc import _connect_chan, Channel
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
    arbiter.

    '''
    actor = current_actor()

    if not actor:
        raise RuntimeError("No actor instance has been defined yet?")

    if actor.is_registrar:
        # we're already the arbiter
        # (likely a re-entrant call from the arbiter actor)
        yield LocalPortal(
            actor,
            Channel((host, port))
        )
    else:
        async with (
            _connect_chan(host, port) as chan,
            open_portal(chan) as regstr_ptl,
        ):
            yield regstr_ptl



# TODO: deprecate and this remove _arbiter form!
@acm
async def get_arbiter(*args, **kwargs):
    warnings.warn(
        '`tractor.get_arbiter()` is now deprecated!\n'
        'Use `.get_registry()` instead!',
        DeprecationWarning,
        stacklevel=2,
    )
    async with get_registry(*args, **kwargs) as to_yield:
        yield to_yield


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


@acm
async def query_actor(
    name: str,
    arbiter_sockaddr: tuple[str, int] | None = None,
    regaddr: tuple[str, int] | None = None,

) -> AsyncGenerator[
    tuple[str, int] | None,
    None,
]:
    '''
    Make a transport address lookup for an actor name to a specific
    registrar.

    Returns the (socket) address or ``None`` if no entry under that
    name exists for the given registrar listening @ `regaddr`.

    '''
    actor: Actor = current_actor()
    if (
        name == 'registrar'
        and actor.is_registrar
    ):
        raise RuntimeError(
            'The current actor IS the registry!?'
        )

    if arbiter_sockaddr is not None:
        warnings.warn(
            '`tractor.query_actor(regaddr=<blah>)` is deprecated.\n'
            'Use `registry_addrs: list[tuple]` instead!',
            DeprecationWarning,
            stacklevel=2,
        )
        regaddr: list[tuple[str, int]] = arbiter_sockaddr

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
async def find_actor(
    name: str,
    arbiter_sockaddr: tuple[str, int]|None = None,
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
    if arbiter_sockaddr is not None:
        warnings.warn(
            '`tractor.find_actor(arbiter_sockaddr=<blah>)` is deprecated.\n'
            'Use `registry_addrs: list[tuple]` instead!',
            DeprecationWarning,
            stacklevel=2,
        )
        registry_addrs: list[tuple[str, int]] = [arbiter_sockaddr]

    @acm
    async def maybe_open_portal_from_reg_addr(
        addr: tuple[str, int],
    ):
        async with query_actor(
            name=name,
            regaddr=addr,
        ) as sockaddr:
            if sockaddr:
                async with _connect_chan(*sockaddr) as chan:
                    async with open_portal(chan) as portal:
                        yield portal
            else:
                yield None

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
        maybe_open_portal_from_reg_addr(addr)
        for addr in registry_addrs
    )

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
    arbiter_sockaddr: tuple[str, int] | None = None,
    registry_addr: tuple[str, int] | None = None,

) -> AsyncGenerator[Portal, None]:
    '''
    Wait on an actor to register with the arbiter.

    A portal to the first registered actor is returned.

    '''
    actor: Actor = current_actor()

    if arbiter_sockaddr is not None:
        warnings.warn(
            '`tractor.wait_for_actor(arbiter_sockaddr=<foo>)` is deprecated.\n'
            'Use `registry_addr: tuple` instead!',
            DeprecationWarning,
            stacklevel=2,
        )
        registry_addr: tuple[str, int] = arbiter_sockaddr

    # TODO: use `.trionics.gather_contexts()` like
    # above in `find_actor()` as well?
    reg_portal: Portal
    regaddr: tuple[str, int] = registry_addr or actor.reg_addrs[0]
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
