"""
tractor: An actor model micro-framework built on
         ``trio`` and ``multiprocessing``.
"""
import importlib
from functools import partial
from typing import Tuple, Any, Optional
import typing

import trio  # type: ignore

from .log import get_console_log, get_logger, get_loglevel
from ._ipc import _connect_chan, Channel
from ._actor import (
    Actor, _start_actor, Arbiter, get_arbiter, find_actor, wait_for_actor
)
from ._trionics import open_nursery
from ._state import current_actor
from ._portal import RemoteActorError


__all__ = [
    'current_actor',
    'find_actor',
    'get_arbiter',
    'wait_for_actor',
    'open_nursery',
    'RemoteActorError',
    'Channel',
]


# set at startup and after forks
_default_arbiter_host = '127.0.0.1'
_default_arbiter_port = 1616


async def _main(
    async_fn: typing.Callable[..., typing.Awaitable],
    args: Tuple,
    kwargs: typing.Dict[str, typing.Any],
    name: str,
    arbiter_addr: Tuple[str, int]
) -> typing.Any:
    """Async entry point for ``tractor``.
    """
    log = get_logger('tractor')
    main = partial(async_fn, *args)
    arbiter_addr = (host, port) = arbiter_addr or (
            _default_arbiter_host, _default_arbiter_port)
    get_console_log(kwargs.get('loglevel', get_loglevel()))

    # make a temporary connection to see if an arbiter exists
    arbiter_found = False
    try:
        async with _connect_chan(host, port):
            arbiter_found = True
    except OSError:
        log.warning(f"No actor could be found @ {host}:{port}")

    # create a local actor and start up its main routine/task
    if arbiter_found:  # we were able to connect to an arbiter
        log.info(f"Arbiter seems to exist @ {host}:{port}")
        actor = Actor(
            name or 'anonymous',
            arbiter_addr=arbiter_addr,
            **kwargs
        )
        host, port = (host, 0)
    else:
        # start this local actor as the arbiter
        actor = Arbiter(
            name or 'arbiter', arbiter_addr=arbiter_addr, **kwargs)

    # ``Actor._async_main()`` creates an internal nursery if one is not
    # provided and thus blocks here until it's main task completes.
    # Note that if the current actor is the arbiter it is desirable
    # for it to stay up indefinitely until a re-election process has
    # taken place - which is not implemented yet FYI).
    return await _start_actor(
        actor, main, host, port, arbiter_addr=arbiter_addr)


def run(
    async_fn: typing.Callable[..., typing.Awaitable],
    *args: Tuple,
    name: str = None,
    arbiter_addr: Tuple[str, int] = (
        _default_arbiter_host, _default_arbiter_port),
    **kwargs: typing.Dict[str, typing.Any],
) -> Any:
    """Run a trio-actor async function in process.

    This is tractor's main entry and the start point for any async actor.
    """
    return trio.run(_main, async_fn, args, kwargs, name, arbiter_addr)


def run_daemon(
    rpc_module_paths: Tuple[str],
    **kwargs
) -> None:
    """Spawn daemon actor which will respond to RPC.

    This is a convenience wrapper around
    ``tractor.run(trio.sleep(float('inf')))`` such that the first actor spawned
    is meant to run forever handling to RPC requests.
    """
    kwargs['rpc_module_paths'] = rpc_module_paths

    for path in rpc_module_paths:
        importlib.import_module(path)

    return run(partial(trio.sleep, float('inf')), **kwargs)
