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
import multiprocessing as mp
import os
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
from .devx import (
    _frame_stack,
    pformat,
)
from .to_asyncio import run_as_asyncio_guest
from ._addr import UnwrappedAddress
from ._runtime import (
    async_main,
    Actor,
)

if TYPE_CHECKING:
    from ._spawn import SpawnMethodKey


log = get_logger(__name__)


def _mp_main(

    actor: Actor,
    accept_addrs: list[UnwrappedAddress],
    forkserver_info: tuple[Any, Any, Any, Any, Any],
    start_method: SpawnMethodKey,
    parent_addr: UnwrappedAddress | None = None,
    infect_asyncio: bool = False,

) -> None:
    '''
    The routine called *after fork* which invokes a fresh `trio.run()`

    '''
    actor._forkserver_info = forkserver_info
    from ._spawn import try_set_start_method
    spawn_ctx: mp.context.BaseContext = try_set_start_method(start_method)
    assert spawn_ctx

    if actor.loglevel is not None:
        log.info(
            f'Setting loglevel for {actor.uid} to {actor.loglevel}'
        )
        get_console_log(actor.loglevel)

    # TODO: use scops headers like for `trio` below!
    # (well after we libify it maybe..)
    log.info(
        f'Started new {spawn_ctx.current_process()} for {actor.uid}'
    #     f"parent_addr is {parent_addr}"
    )
    _state._current_actor: Actor = actor
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
        log.info(
            f'`mp`-subactor {actor.uid} exited'
        )


def _trio_main(
    actor: Actor,
    *,
    parent_addr: UnwrappedAddress|None = None,
    infect_asyncio: bool = False,

) -> None:
    '''
    Entry point for a `trio_run_in_process` subactor.

    '''
    _frame_stack.hide_runtime_frames()

    _state._current_actor = actor
    trio_main = partial(
        async_main,
        actor,
        parent_addr=parent_addr
    )

    if actor.loglevel is not None:
        get_console_log(actor.loglevel)
        actor_info: str = (
            f'|_{actor}\n'
            f'  uid: {actor.uid}\n'
            f'  pid: {os.getpid()}\n'
            f'  parent_addr: {parent_addr}\n'
            f'  loglevel: {actor.loglevel}\n'
        )
        log.info(
            'Starting new `trio` subactor:\n'
            +
            pformat.nest_from_op(
                input_op='>(',  # see syntax ideas above
                tree_str=actor_info,
                back_from_op=2,  # since "complete"
            )
        )
    logmeth = log.info
    exit_status: str = (
        'Subactor exited\n'
        +
        pformat.nest_from_op(
            input_op=')>',  # like a "closed-to-play"-icon from super perspective
            tree_str=actor_info,
            back_from_op=1,
        )
    )
    try:
        if infect_asyncio:
            actor._infected_aio = True
            run_as_asyncio_guest(trio_main)
        else:
            trio.run(trio_main)

    except KeyboardInterrupt:
        logmeth = log.cancel
        exit_status: str = (
            'Actor received KBI (aka an OS-cancel)\n'
            +
            pformat.nest_from_op(
                input_op='c)>',  # closed due to cancel (see above)
                tree_str=actor_info,
            )
        )
    except BaseException as err:
        logmeth = log.error
        exit_status: str = (
            'Main actor task exited due to crash?\n'
            +
            pformat.nest_from_op(
                input_op='x)>',  # closed by error
                tree_str=actor_info,
            )
        )
        # NOTE since we raise a tb will already be shown on the
        # console, thus we do NOT use `.exception()` above.
        raise err

    finally:
        logmeth(exit_status)
