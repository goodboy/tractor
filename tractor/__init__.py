"""
tractor: An actor model micro-framework built on
         ``trio`` and ``multiprocessing``.
"""
import importlib
from functools import partial
from typing import Tuple, Any, Optional, List
import typing

import trio  # type: ignore
from trio import MultiError

from . import log
from ._ipc import _connect_chan, Channel
from ._streaming import Context, stream
from ._discovery import get_arbiter, find_actor, wait_for_actor
from ._actor import Actor, _start_actor, Arbiter
from ._trionics import open_nursery
from ._state import current_actor
from . import _state
from ._exceptions import RemoteActorError, ModuleNotExposed
from ._debug import breakpoint, post_mortem
from . import _spawn
from . import msg
from . import to_asyncio


__all__ = [
    'breakpoint',
    'post_mortem',
    'current_actor',
    'find_actor',
    'get_arbiter',
    'open_nursery',
    'wait_for_actor',
    'Channel',
    'Context',
    'stream',
    'MultiError',
    'RemoteActorError',
    'ModuleNotExposed',
    'msg',
    'to_asyncio',
]


# set at startup and after forks
_default_arbiter_host = '127.0.0.1'
_default_arbiter_port = 1616


async def _main(
    async_fn: typing.Callable[..., typing.Awaitable],
    args: Tuple,
    arbiter_addr: Tuple[str, int],
    name: Optional[str] = None,
    start_method: Optional[str] = None,
    debug_mode: bool = False,
    **kwargs,
) -> typing.Any:
    """Async entry point for ``tractor``.
    """
    logger = log.get_logger('tractor')

    # mark top most level process as root actor
    _state._runtime_vars['_is_root'] = True

    if start_method is not None:
        _spawn.try_set_start_method(start_method)

    if debug_mode and _spawn._spawn_method == 'trio':
        _state._runtime_vars['_debug_mode'] = True

        # expose internal debug module to every actor allowing
        # for use of ``await tractor.breakpoint()``
        kwargs.setdefault('rpc_module_paths', []).append('tractor._debug')

    elif debug_mode:
        raise RuntimeError(
            "Debug mode is only supported for the `trio` backend!"
        )

    main = partial(async_fn, *args)

    arbiter_addr = (host, port) = arbiter_addr or (
        _default_arbiter_host,
        _default_arbiter_port
    )

    loglevel = kwargs.get('loglevel', log.get_loglevel())
    if loglevel is not None:
        log._default_loglevel = loglevel
        log.get_console_log(loglevel)

    # make a temporary connection to see if an arbiter exists
    arbiter_found = False
    try:
        async with _connect_chan(host, port):
            arbiter_found = True
    except OSError:
        logger.warning(f"No actor could be found @ {host}:{port}")

    # create a local actor and start up its main routine/task
    if arbiter_found:  # we were able to connect to an arbiter
        logger.info(f"Arbiter seems to exist @ {host}:{port}")
        actor = Actor(
            name or 'anonymous',
            arbiter_addr=arbiter_addr,
            **kwargs
        )
        host, port = (host, 0)
    else:
        # start this local actor as the arbiter
        actor = Arbiter(
            name or 'arbiter',
            arbiter_addr=arbiter_addr,
            **kwargs
        )

    # ``Actor._async_main()`` creates an internal nursery if one is not
    # provided and thus blocks here until it's main task completes.
    # Note that if the current actor is the arbiter it is desirable
    # for it to stay up indefinitely until a re-election process has
    # taken place - which is not implemented yet FYI).
    return await _start_actor(
        actor, main, host, port, arbiter_addr=arbiter_addr
    )


def run(
    async_fn: typing.Callable[..., typing.Awaitable],
    *args,
    name: Optional[str] = None,
    arbiter_addr: Tuple[str, int] = (
        _default_arbiter_host,
        _default_arbiter_port,
    ),
    # either the `multiprocessing` start method:
    # https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods
    # OR `trio` (the new default).
    start_method: Optional[str] = None,
    debug_mode: bool = False,
    **kwargs,
) -> Any:
    """Run a trio-actor async function in process.

    This is tractor's main entry and the start point for any async actor.
    """
    return trio.run(
        partial(
            # our entry
            _main,

            # user entry point
            async_fn,
            args,

            # global kwargs
            arbiter_addr=arbiter_addr,
            name=name,
            start_method=start_method,
            debug_mode=debug_mode,
            **kwargs,
        )
    )


def run_daemon(
    rpc_module_paths: List[str],
    **kwargs
) -> None:
    """Spawn daemon actor which will respond to RPC.

    This is a convenience wrapper around
    ``tractor.run(trio.sleep(float('inf')))`` such that the first actor spawned
    is meant to run forever responding to RPC requests.
    """
    kwargs['rpc_module_paths'] = list(rpc_module_paths)

    for path in rpc_module_paths:
        importlib.import_module(path)

    return run(partial(trio.sleep, float('inf')), **kwargs)
