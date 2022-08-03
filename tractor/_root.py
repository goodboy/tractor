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

'''
Root actor runtime ignition(s).

'''
from contextlib import asynccontextmanager
from functools import partial
import importlib
import logging
import os
from typing import Tuple, Optional, List, Any
import typing
import warnings

import trio

from ._actor import Actor, Arbiter
from . import _debug
from . import _spawn
from . import _state
from . import log
from ._ipc import _connect_chan
from ._exceptions import is_multi_cancelled


# set at startup and after forks
_default_arbiter_host: str = '127.0.0.1'
_default_arbiter_port: int = 1616


logger = log.get_logger('tractor')


@asynccontextmanager
async def open_root_actor(

    # defaults are above
    arbiter_addr: Optional[Tuple[str, int]] = (
        _default_arbiter_host,
        _default_arbiter_port,
    ),

    name: Optional[str] = 'root',

    # either the `multiprocessing` start method:
    # https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods
    # OR `trio` (the new default).
    start_method: Optional[str] = None,

    # enables the multi-process debugger support
    debug_mode: bool = False,

    # internal logging
    loglevel: Optional[str] = None,

    enable_modules: Optional[List] = None,
    rpc_module_paths: Optional[List] = None,

) -> typing.Any:
    """Async entry point for ``tractor``.

    """
    # Override the global debugger hook to make it play nice with
    # ``trio``, see:
    # https://github.com/python-trio/trio/issues/1155#issuecomment-742964018
    os.environ['PYTHONBREAKPOINT'] = 'tractor._debug._set_trace'

    # mark top most level process as root actor
    _state._runtime_vars['_is_root'] = True

    # caps based rpc list
    enable_modules = enable_modules or []

    if rpc_module_paths:
        warnings.warn(
            "`rpc_module_paths` is now deprecated, use "
            " `enable_modules` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        enable_modules.extend(rpc_module_paths)

    if start_method is not None:
        _spawn.try_set_start_method(start_method)

    arbiter_addr = (host, port) = arbiter_addr or (
        _default_arbiter_host,
        _default_arbiter_port,
    )

    loglevel = (loglevel or log._default_loglevel).upper()

    if debug_mode and _spawn._spawn_method == 'trio':
        _state._runtime_vars['_debug_mode'] = True

        # expose internal debug module to every actor allowing
        # for use of ``await tractor.breakpoint()``
        enable_modules.append('tractor._debug')

        # if debug mode get's enabled *at least* use that level of
        # logging for some informative console prompts.
        if (
            logging.getLevelName(
                # lul, need the upper case for the -> int map?
                # sweet "dynamic function behaviour" stdlib...
                loglevel,
            ) > logging.getLevelName('PDB')
        ):
            loglevel = 'PDB'

    elif debug_mode:
        raise RuntimeError(
            "Debug mode is only supported for the `trio` backend!"
        )

    log.get_console_log(loglevel)

    try:
        # make a temporary connection to see if an arbiter exists,
        # if one can't be made quickly we assume none exists.
        arbiter_found = False

        # TODO: this connect-and-bail forces us to have to carefully
        # rewrap TCP 104-connection-reset errors as EOF so as to avoid
        # propagating cancel-causing errors to the channel-msg loop
        # machinery.  Likely it would be better to eventually have
        # a "discovery" protocol with basic handshake instead.
        with trio.move_on_after(1):
            async with _connect_chan(host, port):
                arbiter_found = True

    except OSError:
        # TODO: make this a "discovery" log level?
        logger.warning(f"No actor could be found @ {host}:{port}")

    # create a local actor and start up its main routine/task
    if arbiter_found:

        # we were able to connect to an arbiter
        logger.info(f"Arbiter seems to exist @ {host}:{port}")

        actor = Actor(
            name or 'anonymous',
            arbiter_addr=arbiter_addr,
            loglevel=loglevel,
            enable_modules=enable_modules,
        )
        host, port = (host, 0)

    else:
        # start this local actor as the arbiter (aka a regular actor who
        # manages the local registry of "mailboxes")

        # Note that if the current actor is the arbiter it is desirable
        # for it to stay up indefinitely until a re-election process has
        # taken place - which is not implemented yet FYI).

        actor = Arbiter(
            name or 'arbiter',
            arbiter_addr=arbiter_addr,
            loglevel=loglevel,
            enable_modules=enable_modules,
        )

    try:
        # assign process-local actor
        _state._current_actor = actor

        # start local channel-server and fake the portal API
        # NOTE: this won't block since we provide the nursery
        logger.info(f"Starting local {actor} @ {host}:{port}")

        # start the actor runtime in a new task
        async with trio.open_nursery() as nursery:

            # ``Actor._async_main()`` creates an internal nursery and
            # thus blocks here until the entire underlying actor tree has
            # terminated thereby conducting structured concurrency.

            await nursery.start(
                partial(
                    actor._async_main,
                    accept_addr=(host, port),
                    parent_addr=None
                )
            )
            try:
                yield actor

            except (Exception, trio.MultiError) as err:

                entered = await _debug._maybe_enter_pm(err)

                if not entered and not is_multi_cancelled(err):
                    logger.exception("Root actor crashed:")

                # always re-raise
                raise

            finally:
                # NOTE: not sure if we'll ever need this but it's
                # possibly better for even more determinism?
                # logger.cancel(
                #     f'Waiting on {len(nurseries)} nurseries in root..')
                # nurseries = actor._actoruid2nursery.values()
                # async with trio.open_nursery() as tempn:
                #     for an in nurseries:
                #         tempn.start_soon(an.exited.wait)

                logger.cancel("Shutting down root actor")
                await actor.cancel()
    finally:
        _state._current_actor = None
        logger.runtime("Root actor terminated")


def run(

    # target
    async_fn: typing.Callable[..., typing.Awaitable],
    *args,

    # runtime kwargs
    name: Optional[str] = 'root',
    arbiter_addr: Tuple[str, int] = (
        _default_arbiter_host,
        _default_arbiter_port,
    ),

    start_method: Optional[str] = None,
    debug_mode: bool = False,
    **kwargs,

) -> Any:
    """Run a trio-actor async function in process.

    This is tractor's main entry and the start point for any async actor.
    """
    async def _main():

        async with open_root_actor(
            arbiter_addr=arbiter_addr,
            name=name,
            start_method=start_method,
            debug_mode=debug_mode,
            **kwargs,
        ):

            return await async_fn(*args)

    warnings.warn(
        "`tractor.run()` is now deprecated. `tractor` now"
        " implicitly starts the root actor on first actor nursery"
        " use. If you want to start the root actor manually, use"
        " `tractor.open_root_actor()`.",
        DeprecationWarning,
        stacklevel=2,
    )
    return trio.run(_main)


def run_daemon(
    enable_modules: list[str],
    **kwargs
) -> None:
    '''
    Spawn daemon actor which will respond to RPC.

    This is a convenience wrapper around
    ``tractor.run(trio.sleep(float('inf')))`` such that the first actor spawned
    is meant to run forever responding to RPC requests.

    '''
    kwargs['enable_modules'] = list(enable_modules)

    for path in enable_modules:
        importlib.import_module(path)

    return run(partial(trio.sleep, float('inf')), **kwargs)
