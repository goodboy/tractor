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
Per process state

"""
from __future__ import annotations
from contextvars import (
    ContextVar,
)
from typing import (
    Any,
    TYPE_CHECKING,
)

from trio.lowlevel import current_task

if TYPE_CHECKING:
    from ._runtime import Actor
    from ._context import Context


_current_actor: Actor|None = None  # type: ignore # noqa
_last_actor_terminated: Actor|None = None

# TODO: mk this a `msgspec.Struct`!
_runtime_vars: dict[str, Any] = {
    '_debug_mode': False,
    '_is_root': False,
    '_root_mailbox': (None, None),
    '_registry_addrs': [],

    # for `tractor.pause_from_sync()` & `breakpoint()` support
    'use_greenback': False,
}


def last_actor() -> Actor|None:
    '''
    Try to return last active `Actor` singleton
    for this process.

    For case where runtime already exited but someone is asking
    about the "last" actor probably to get its `.uid: tuple`.

    '''
    return _last_actor_terminated


def current_actor(
    err_on_no_runtime: bool = True,
) -> Actor:
    '''
    Get the process-local actor instance.

    '''
    if (
        err_on_no_runtime
        and _current_actor is None
    ):
        msg: str = 'No local actor has been initialized yet?\n'
        from ._exceptions import NoRuntime

        if last := last_actor():
            msg += (
                f'Apparently the lact active actor was\n'
                f'|_{last}\n'
                f'|_{last.uid}\n'
            )
        # no actor runtime has (as of yet) ever been started for
        # this process.
        else:
            msg += (
                # 'No last actor found?\n'
                '\nDid you forget to call one of,\n'
                '- `tractor.open_root_actor()`\n'
                '- `tractor.open_nursery()`\n'
            )

        raise NoRuntime(msg)

    return _current_actor


def is_main_process() -> bool:
    '''
    Bool determining if this actor is running in the top-most process.

    '''
    import multiprocessing as mp
    return mp.current_process().name == 'MainProcess'


def debug_mode() -> bool:
    '''
    Bool determining if "debug mode" is on which enables
    remote subactor pdb entry on crashes.

    '''
    return bool(_runtime_vars['_debug_mode'])


def is_root_process() -> bool:
    return _runtime_vars['_is_root']


_ctxvar_Context: ContextVar[Context] = ContextVar(
    'ipc_context',
    default=None,
)


def current_ipc_ctx(
    error_on_not_set: bool = False,
) -> Context|None:
    ctx: Context = _ctxvar_Context.get()

    if (
        not ctx
        and error_on_not_set
    ):
        from ._exceptions import InternalError
        raise InternalError(
            'No IPC context has been allocated for this task yet?\n'
            f'|_{current_task()}\n'
        )
    return ctx
