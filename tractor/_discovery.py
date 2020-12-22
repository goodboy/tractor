"""
Actor discovery API.
"""
import typing
from typing import Tuple, Optional, Union
from async_generator import asynccontextmanager

from ._ipc import _connect_chan, Channel
from ._portal import (
    Portal,
    open_portal,
    LocalPortal,
)
from ._state import current_actor, _runtime_vars


@asynccontextmanager
async def get_arbiter(
    host: str,
    port: int,
) -> typing.AsyncGenerator[Union[Portal, LocalPortal], None]:
    """Return a portal instance connected to a local or remote
    arbiter.
    """
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


@asynccontextmanager
async def get_root(
**kwargs,
) -> typing.AsyncGenerator[Union[Portal, LocalPortal], None]:
    host, port = _runtime_vars['_root_mailbox']
    assert host is not None
    async with _connect_chan(host, port) as chan:
        async with open_portal(chan, **kwargs) as portal:
            yield portal


@asynccontextmanager
async def find_actor(
    name: str,
    arbiter_sockaddr: Tuple[str, int] = None
) -> typing.AsyncGenerator[Optional[Portal], None]:
    """Ask the arbiter to find actor(s) by name.

    Returns a connected portal to the last registered matching actor
    known to the arbiter.
    """
    actor = current_actor()
    async with get_arbiter(*arbiter_sockaddr or actor._arb_addr) as arb_portal:
        sockaddr = await arb_portal.run_from_ns('self', 'find_actor', name=name)
        # TODO: return portals to all available actors - for now just
        # the last one that registered
        if name == 'arbiter' and actor.is_arbiter:
            raise RuntimeError("The current actor is the arbiter")
        elif sockaddr:
            async with _connect_chan(*sockaddr) as chan:
                async with open_portal(chan) as portal:
                    yield portal
        else:
            yield None


@asynccontextmanager
async def wait_for_actor(
    name: str,
    arbiter_sockaddr: Tuple[str, int] = None
) -> typing.AsyncGenerator[Portal, None]:
    """Wait on an actor to register with the arbiter.

    A portal to the first registered actor is returned.
    """
    actor = current_actor()
    async with get_arbiter(*arbiter_sockaddr or actor._arb_addr) as arb_portal:
        sockaddrs = await arb_portal.run_from_ns('self', 'wait_for_actor', name=name)
        sockaddr = sockaddrs[-1]
        async with _connect_chan(*sockaddr) as chan:
            async with open_portal(chan) as portal:
                yield portal
