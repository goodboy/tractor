# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public License
# as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.

'''
Debugger synchronization APIs to ensure orderly access and
non-TTY-clobbering graceful teardown.


'''
from __future__ import annotations
from contextlib import (
    asynccontextmanager as acm,
)
from functools import (
    partial,
)
from typing import (
    AsyncGenerator,
    Callable,
)

from tractor.log import get_logger
import trio
from trio.lowlevel import (
    current_task,
    Task,
)
from tractor._context import Context
from tractor._state import (
    current_actor,
    debug_mode,
    is_root_process,
)
from ._repl import (
    TractorConfig as TractorConfig,
)
from ._tty_lock import (
    Lock,
    request_root_stdio_lock,
    any_connected_locker_child,
)
from ._sigint import (
    sigint_shield as sigint_shield,
    _ctlc_ignore_header as _ctlc_ignore_header
)

log = get_logger()


async def maybe_wait_for_debugger(
    poll_steps: int = 2,
    poll_delay: float = 0.1,
    child_in_debug: bool = False,

    header_msg: str = '',
    _ll: str = 'devx',

) -> bool:  # was locked and we polled?

    if (
        not debug_mode()
        and
        not child_in_debug
    ):
        return False

    logmeth: Callable = getattr(log, _ll)

    msg: str = header_msg
    if (
        is_root_process()
    ):
        # If we error in the root but the debugger is
        # engaged we don't want to prematurely kill (and
        # thus clobber access to) the local tty since it
        # will make the pdb repl unusable.
        # Instead try to wait for pdb to be released before
        # tearing down.
        ctx_in_debug: Context|None = Lock.ctx_in_debug
        in_debug: tuple[str, str]|None = (
            ctx_in_debug.chan.uid
            if ctx_in_debug
            else None
        )
        if in_debug == current_actor().uid:
            log.debug(
                msg
                +
                'Root already owns the TTY LOCK'
            )
            return True

        elif in_debug:
            msg += (
                f'Debug `Lock` in use by subactor\n|\n|_{in_debug}\n'
            )
            # TODO: could this make things more deterministic?
            # wait to see if a sub-actor task will be
            # scheduled and grab the tty lock on the next
            # tick?
            # XXX => but it doesn't seem to work..
            # await trio.testing.wait_all_tasks_blocked(cushion=0)
        else:
            logmeth(
                msg
                +
                'Root immediately acquired debug TTY LOCK'
            )
            return False

        for istep in range(poll_steps):
            if (
                Lock.req_handler_finished is not None
                and not Lock.req_handler_finished.is_set()
                and in_debug is not None
            ):
                # caller_frame_info: str = pformat_caller_frame()
                logmeth(
                    msg
                    +
                    '\n^^ Root is waiting on tty lock release.. ^^\n'
                    # f'{caller_frame_info}\n'
                )

                if not any_connected_locker_child():
                    Lock.get_locking_task_cs().cancel()

                with trio.CancelScope(shield=True):
                    await Lock.req_handler_finished.wait()

                log.devx(
                    f'Subactor released debug lock\n'
                    f'|_{in_debug}\n'
                )
                break

            # is no subactor locking debugger currently?
            if (
                in_debug is None
                and (
                    Lock.req_handler_finished is None
                    or Lock.req_handler_finished.is_set()
                )
            ):
                logmeth(
                    msg
                    +
                    'Root acquired tty lock!'
                )
                break

            else:
                logmeth(
                    'Root polling for debug:\n'
                    f'poll step: {istep}\n'
                    f'poll delya: {poll_delay}\n\n'
                    f'{Lock.repr()}\n'
                )
                with trio.CancelScope(shield=True):
                    await trio.sleep(poll_delay)
                    continue

        return True

    # else:
    #     # TODO: non-root call for #320?
    #     this_uid: tuple[str, str] = current_actor().uid
    #     async with acquire_debug_lock(
    #         subactor_uid=this_uid,
    #     ):
    #         pass
    return False


@acm
async def acquire_debug_lock(
    subactor_uid: tuple[str, str],
) -> AsyncGenerator[
    trio.CancelScope|None,
    tuple,
]:
    '''
    Request to acquire the TTY `Lock` in the root actor, release on
    exit.

    This helper is for actor's who don't actually need to acquired
    the debugger but want to wait until the lock is free in the
    process-tree root such that they don't clobber an ongoing pdb
    REPL session in some peer or child!

    '''
    if not debug_mode():
        yield None
        return

    task: Task = current_task()
    async with trio.open_nursery() as n:
        ctx: Context = await n.start(
            partial(
                request_root_stdio_lock,
                actor_uid=subactor_uid,
                task_uid=(task.name, id(task)),
            )
        )
        yield ctx
        ctx.cancel()
