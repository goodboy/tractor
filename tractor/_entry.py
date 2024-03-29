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
Sub-process entry points.

"""
from __future__ import annotations
from functools import partial
from typing import (
    Any,
    TYPE_CHECKING,
)

import trio  # type: ignore

from .log import (
    get_console_log,
    get_logger,
)
from . import _state
from .to_asyncio import run_as_asyncio_guest
from ._runtime import (
    async_main,
    Actor,
)

if TYPE_CHECKING:
    from ._spawn import SpawnMethodKey


log = get_logger(__name__)


def _mp_main(

    actor: Actor,  # type: ignore
    accept_addr: tuple[str, int],
    forkserver_info: tuple[Any, Any, Any, Any, Any],
    start_method: SpawnMethodKey,
    parent_addr: tuple[str, int] | None = None,
    infect_asyncio: bool = False,

) -> None:
    '''
    The routine called *after fork* which invokes a fresh ``trio.run``

    '''
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
        async_main,
        actor,
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

    finally:
        log.info(f"Actor {actor.uid} terminated")


def _trio_main(

    actor: Actor,  # type: ignore
    *,
    parent_addr: tuple[str, int] | None = None,
    infect_asyncio: bool = False,

) -> None:
    '''
    Entry point for a `trio_run_in_process` subactor.

    '''
    log.info(f"Started new trio process for {actor.uid}")

    if actor.loglevel is not None:
        log.info(
            f"Setting loglevel for {actor.uid} to {actor.loglevel}")
        get_console_log(actor.loglevel)

    log.info(
        f"Started {actor.uid}")

    _state._current_actor = actor

    log.debug(f"parent_addr is {parent_addr}")
    trio_main = partial(
        async_main,
        actor,
        parent_addr=parent_addr
    )

    try:
        if infect_asyncio:
            actor._infected_aio = True
            run_as_asyncio_guest(trio_main)
        else:
            trio.run(trio_main)
    except KeyboardInterrupt:
        log.warning(f"Actor {actor.uid} received KBI")

    finally:
        log.info(f"Actor {actor.uid} terminated")
