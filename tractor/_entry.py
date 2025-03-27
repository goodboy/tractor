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
import os
import textwrap
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
from .devx import _debug
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
    The routine called *after fork* which invokes a fresh `trio.run()`

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
        log.info(f"Subactor {actor.uid} terminated")


# TODO: move this func to some kinda `.devx._conc_lang.py` eventually
# as we work out our multi-domain state-flow-syntax!
def nest_from_op(
    input_op: str,
    #
    # ?TODO? an idea for a syntax to the state of concurrent systems
    # as a "3-domain" (execution, scope, storage) model and using
    # a minimal ascii/utf-8 operator-set.
    #
    # try not to take any of this seriously yet XD
    #
    # > is a "play operator" indicating (CPU bound)
    #   exec/work/ops required at the "lowest level computing"
    #
    # execution primititves (tasks, threads, actors..) denote their
    # lifetime with '(' and ')' since parentheses normally are used
    # in many langs to denote function calls.
    #
    # starting = (
    # >(  opening/starting; beginning of the thread-of-exec (toe?)
    # (>  opened/started,  (finished spawning toe)
    # |_<Task: blah blah..>  repr of toe, in py these look like <objs>
    #
    # >) closing/exiting/stopping,
    # )> closed/exited/stopped,
    # |_<Task: blah blah..>
    #   [OR <), )< ?? ]
    #
    # ending = )
    # >c) cancelling to close/exit
    # c)> cancelled (caused close), OR?
    #  |_<Actor: ..>
    #   OR maybe "<c)" which better indicates the cancel being
    #   "delivered/returned" / returned" to LHS?
    #
    # >x)  erroring to eventuall exit
    # x)>  errored and terminated
    #  |_<Actor: ...>
    #
    # scopes: supers/nurseries, IPC-ctxs, sessions, perms, etc.
    # >{  opening
    # {>  opened
    # }>  closed
    # >}  closing
    #
    # storage: like queues, shm-buffers, files, etc..
    # >[  opening
    # [>  opened
    #  |_<FileObj: ..>
    #
    # >]  closing
    # ]>  closed

    # IPC ops: channels, transports, msging
    # =>  req msg
    # <=  resp msg
    # <=> 2-way streaming (of msgs)
    # <-  recv 1 msg
    # ->  send 1 msg
    #
    # TODO: still not sure on R/L-HS approach..?
    # =>(  send-req to exec start (task, actor, thread..)
    # (<=  recv-req to ^
    #
    # (<=  recv-req ^
    # <=(  recv-resp opened remote exec primitive
    # <=)  recv-resp closed
    #
    # )<=c req to stop due to cancel
    # c=>) req to stop due to cancel
    #
    # =>{  recv-req to open
    # <={  send-status that it closed

    tree_str: str,

    # NOTE: so move back-from-the-left of the `input_op` by
    # this amount.
    back_from_op: int = 0,
) -> str:
    '''
    Depth-increment the input (presumably hierarchy/supervision)
    input "tree string" below the provided `input_op` execution
    operator, so injecting a `"\n|_{input_op}\n"`and indenting the
    `tree_str` to nest content aligned with the ops last char.

    '''
    return (
        f'{input_op}\n'
        +
        textwrap.indent(
            tree_str,
            prefix=(
                len(input_op)
                -
                (back_from_op + 1)
            ) * ' ',
        )
    )


def _trio_main(
    actor: Actor,
    *,
    parent_addr: tuple[str, int] | None = None,
    infect_asyncio: bool = False,

) -> None:
    '''
    Entry point for a `trio_run_in_process` subactor.

    '''
    _debug.hide_runtime_frames()

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
            nest_from_op(
                input_op='>(',  # see syntax ideas above
                tree_str=actor_info,
                back_from_op=1,
            )
        )
    logmeth = log.info
    exit_status: str = (
        'Subactor exited\n'
        +
        nest_from_op(
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
            nest_from_op(
                input_op='c)>',  # closed due to cancel (see above)
                tree_str=actor_info,
            )
        )
    except BaseException as err:
        logmeth = log.error
        exit_status: str = (
            'Main actor task exited due to crash?\n'
            +
            nest_from_op(
                input_op='x)>',  # closed by error
                tree_str=actor_info,
            )
        )
        # NOTE since we raise a tb will already be shown on the
        # console, thus we do NOT use `.exception()` above.
        raise err

    finally:
        logmeth(exit_status)
