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
Actor discovery API.

"""
from typing import Tuple, Optional, Union, AsyncGenerator
from contextlib import asynccontextmanager as acm

from ._ipc import _connect_chan, Channel
from ._portal import (
    Portal,
    open_portal,
    LocalPortal,
)
from ._state import current_actor, _runtime_vars


@acm
async def get_arbiter(

    host: str,
    port: int,

) -> AsyncGenerator[Union[Portal, LocalPortal], None]:
    '''Return a portal instance connected to a local or remote
    arbiter.
    '''
    actor = current_actor()

    if not actor:
        raise RuntimeError("No actor instance has been defined yet?")

    if actor.is_arbiter:
        # we're already the arbiter
        # (likely a re-entrant call from the arbiter actor)
        yield LocalPortal(actor, Channel((host, port)))
    else:
        async with _connect_chan(host, port) as chan:

            async with open_portal(chan) as arb_portal:

                yield arb_portal


@acm
async def get_root(
    **kwargs,
) -> AsyncGenerator[Portal, None]:

    host, port = _runtime_vars['_root_mailbox']
    assert host is not None

    async with _connect_chan(host, port) as chan:
        async with open_portal(chan, **kwargs) as portal:
            yield portal


@acm
async def query_actor(
    name: str,
    arbiter_sockaddr: Optional[tuple[str, int]] = None,

) -> AsyncGenerator[tuple[str, int], None]:
    '''
    Simple address lookup for a given actor name.

    Returns the (socket) address or ``None``.

    '''
    actor = current_actor()
    async with get_arbiter(
        *arbiter_sockaddr or actor._arb_addr
    ) as arb_portal:

        sockaddr = await arb_portal.run_from_ns(
            'self',
            'find_actor',
            name=name,
        )

        # TODO: return portals to all available actors - for now just
        # the last one that registered
        if name == 'arbiter' and actor.is_arbiter:
            raise RuntimeError("The current actor is the arbiter")

        yield sockaddr if sockaddr else None


@acm
async def find_actor(
    name: str,
    arbiter_sockaddr: Tuple[str, int] = None

) -> AsyncGenerator[Optional[Portal], None]:
    '''
    Ask the arbiter to find actor(s) by name.

    Returns a connected portal to the last registered matching actor
    known to the arbiter.

    '''
    async with query_actor(
        name=name,
        arbiter_sockaddr=arbiter_sockaddr,
    ) as sockaddr:

        if sockaddr:
            async with _connect_chan(*sockaddr) as chan:
                async with open_portal(chan) as portal:
                    yield portal
        else:
            yield None


@acm
async def wait_for_actor(
    name: str,
    arbiter_sockaddr: Tuple[str, int] = None
) -> AsyncGenerator[Portal, None]:
    """Wait on an actor to register with the arbiter.

    A portal to the first registered actor is returned.
    """
    actor = current_actor()

    async with get_arbiter(
        *arbiter_sockaddr or actor._arb_addr,
    ) as arb_portal:
        sockaddrs = await arb_portal.run_from_ns(
            'self',
            'wait_for_actor',
            name=name,
        )
        sockaddr = sockaddrs[-1]

        async with _connect_chan(*sockaddr) as chan:
            async with open_portal(chan) as portal:
                yield portal
