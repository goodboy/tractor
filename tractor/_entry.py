"""
Process entry points.
"""
import asyncio
from functools import partial
from typing import Tuple, Any, Awaitable

import trio  # type: ignore

from ._actor import Actor
from .log import get_console_log, get_logger
from . import _state


__all__ = ('run',)


log = get_logger(__name__)


def _asyncio_main(
    trio_main: Awaitable,
) -> None:
    """Entry for an "infected ``asyncio`` actor".

    Uh, oh. :o

    It looks like your event loop has caught a case of the ``trio``s.

    :()

    Don't worry, we've heard you'll barely notice. You might hallucinate
    a few more propagating errors and feel like your digestion has
    slowed but if anything get's too bad your parents will know about
    it.

    :)
    """
    async def aio_main(trio_main):
        loop = asyncio.get_running_loop()

        trio_done_fut = asyncio.Future()

        def trio_done_callback(main_outcome):
            log.info(f"trio_main finished: {main_outcome!r}")
            trio_done_fut.set_result(main_outcome)

        # start the infection: run trio on the asyncio loop in "guest mode"
        log.info(f"Infecting asyncio process with {trio_main}")
        trio.lowlevel.start_guest_run(
            trio_main,
            run_sync_soon_threadsafe=loop.call_soon_threadsafe,
            done_callback=trio_done_callback,
        )

        (await trio_done_fut).unwrap()

    asyncio.run(aio_main(trio_main))


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
            _asyncio_main(trio_main)
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
