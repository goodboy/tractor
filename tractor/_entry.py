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

    actor: Actor,
    accept_addrs: list[tuple[str, int]],
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
        actor=actor,
        accept_addrs=accept_addrs,
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

    actor: Actor,
    *,
    parent_addr: tuple[str, int] | None = None,
    infect_asyncio: bool = False,

) -> None:
    '''
    Entry point for a `trio_run_in_process` subactor.

    '''
    __tracebackhide__: bool = True
    _state._current_actor = actor
    trio_main = partial(
        async_main,
        actor,
        parent_addr=parent_addr
    )

    if actor.loglevel is not None:
        get_console_log(actor.loglevel)
        import os
        actor_info: str = (
            f'|_{actor}\n'
            f'  uid: {actor.uid}\n'
            f'  pid: {os.getpid()}\n'
            f'  parent_addr: {parent_addr}\n'
            f'  loglevel: {actor.loglevel}\n'
        )
        log.info(
            'Started new trio process:\n'
            +
            actor_info
        )

    try:
        if infect_asyncio:
            actor._infected_aio = True
            run_as_asyncio_guest(trio_main)
        else:
            trio.run(trio_main)
    except KeyboardInterrupt:
        log.cancel(
            'Actor received KBI\n'
            +
            actor_info
        )

    finally:
        log.info(
            'Actor terminated\n'
            +
            actor_info
        )
