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
Per actor-process runtime state mgmt APIs.

'''
from __future__ import annotations
from contextvars import (
    ContextVar,
)
import os
from pathlib import Path
from typing import (
    Any,
    Literal,
    TYPE_CHECKING,
)

from trio.lowlevel import current_task

if TYPE_CHECKING:
    from ._runtime import Actor
    from ._context import Context


# default IPC transport protocol settings
TransportProtocolKey = Literal[
    'tcp',
    'uds',
]
_def_tpt_proto: TransportProtocolKey = 'tcp'

_current_actor: Actor|None = None  # type: ignore # noqa
_last_actor_terminated: Actor|None = None

# TODO: mk this a `msgspec.Struct`!
# -[ ] type out all fields obvi!
# -[ ] (eventually) mk wire-ready for monitoring?
_runtime_vars: dict[str, Any] = {
    # root of actor-process tree info
    '_is_root': False,  # bool
    '_root_mailbox': (None, None),  # tuple[str|None, str|None]
    '_root_addrs': [],  # tuple[str|None, str|None]

    # parent->chld ipc protocol caps
    '_enable_tpts': [_def_tpt_proto],

    # registrar info
    '_registry_addrs': [],

    # `debug_mode: bool` settings
    '_debug_mode': False,  # bool
    'repl_fixture': False,  # |AbstractContextManager[bool]
    # for `tractor.pause_from_sync()` & `breakpoint()` support
    'use_greenback': False,

    # infected-`asyncio`-mode: `trio` running as guest.
    '_is_infected_aio': False,
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
        and
        _current_actor is None
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


def is_root_process() -> bool:
    '''
    Bool determining if this actor is running in the top-most process.

    '''
    import multiprocessing as mp
    return mp.current_process().name == 'MainProcess'


is_main_process = is_root_process


def is_debug_mode() -> bool:
    '''
    Bool determining if "debug mode" is on which enables
    remote subactor pdb entry on crashes.

    '''
    return bool(_runtime_vars['_debug_mode'])


debug_mode = is_debug_mode


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


# std ODE (mutable) app state location
_rtdir: Path = Path(os.environ['XDG_RUNTIME_DIR'])


def get_rt_dir(
    subdir: str = 'tractor'
) -> Path:
    '''
    Return the user "runtime dir" where most userspace apps stick
    their IPC and cache related system util-files; we take hold
    of a `'XDG_RUNTIME_DIR'/tractor/` subdir by default.

    '''
    rtdir: Path = _rtdir / subdir
    if not rtdir.is_dir():
        rtdir.mkdir()
    return rtdir


def current_ipc_protos() -> list[str]:
    '''
    Return the list of IPC transport protocol keys currently
    in use by this actor.

    The keys are as declared by `MsgTransport` and `Address`
    concrete-backend sub-types defined throughout `tractor.ipc`.

    '''
    return _runtime_vars['_enable_tpts']
