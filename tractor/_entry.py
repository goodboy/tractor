"""
Process entry points.
"""
from functools import partial
from typing import Tuple, Any

import trio  # type: ignore

from ._actor import Actor
from .log import get_console_log, get_logger
from . import _state
from .to_asyncio import run_as_asyncio_guest


log = get_logger(__name__)


def _mp_main(
    actor: 'Actor',
    accept_addr: Tuple[str, int],
    forkserver_info: Tuple[Any, Any, Any, Any, Any],
    start_method: str,
    parent_addr: Tuple[str, int] = None,
    infect_asyncio: bool = False,
) -> None:
    """The routine called *after fork* which invokes a fresh ``trio.run``
    """
    actor._forkserver_info = forkserver_info
    from ._spawn import try_set_start_method
    spawn_ctx = try_set_start_method(start_method)

    if actor.loglevel is not None:
        log.info(
            f"Setting loglevel for {actor.uid} to {actor.loglevel}")
        get_console_log(actor.loglevel)

    assert spawn_ctx
    log.info(
        f"Started new {spawn_ctx.current_process()} for {actor.uid}")

    _state._current_actor = actor

    log.debug(f"parent_addr is {parent_addr}")
    trio_main = partial(
        actor._async_main,
        accept_addr,
        parent_addr=parent_addr
    )
    try:
        if infect_asyncio:
            actor._infected_aio = True
            run_as_asyncio_guest(trio_main)
        else:
            trio.run(trio_main)
    except KeyboardInterrupt:
        pass  # handle it the same way trio does?
    log.info(f"Actor {actor.uid} terminated")


async def _trip_main(
    actor: 'Actor',
    accept_addr: Tuple[str, int],
    parent_addr: Tuple[str, int] = None
) -> None:
    """Entry point for a `trio_run_in_process` subactor.

    Here we don't need to call `trio.run()` since trip does that as
    part of its subprocess startup sequence.
    """
    if actor.loglevel is not None:
        log.info(
            f"Setting loglevel for {actor.uid} to {actor.loglevel}")
        get_console_log(actor.loglevel)

    log.info(f"Started new TRIP process for {actor.uid}")
    _state._current_actor = actor
    await actor._async_main(accept_addr, parent_addr=parent_addr)
    log.info(f"Actor {actor.uid} terminated")
