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
from pathlib import Path
from typing import (
    Any,
    Callable,
    Literal,
    TYPE_CHECKING,
)

import platformdirs
from trio.lowlevel import current_task

from msgspec import (
    field,
    Struct,
)

if TYPE_CHECKING:
    from ._runtime import Actor
    from .._context import Context


# default IPC transport protocol settings
TransportProtocolKey = Literal[
    'tcp',
    'uds',
]
_def_tpt_proto: TransportProtocolKey = 'tcp'

_current_actor: Actor|None = None  # type: ignore # noqa
_last_actor_terminated: Actor|None = None


# TODO: mk this a `msgspec.Struct`!
# -[x] type out all fields obvi!
# -[ ] (eventually) mk wire-ready for monitoring?
class RuntimeVars(Struct):
    '''
    Actor-(and thus process)-global runtime state.

    This struct is relayed from parent to child during sub-actor
    spawning and is a singleton instance per process.

    Generally contains,
    - root-actor indicator.
    - comms-info: addrs for both (public) process/service-discovery
      and in-tree contact with other actors.
    - transport-layer IPC protocol server(s) settings.
    - debug-mode settings for enabling sync breakpointing and any
      surrounding REPL-fixture hooking.
    - infected-`asyncio` via guest-mode toggle(s)/cohfig.

    '''
    _is_root: bool = False  # bool
    _root_mailbox: tuple[str, str|int] = (None, None)  # tuple[str|None, str|None]
    _root_addrs: list[
        tuple[str, str|int],
    ] = []  # tuple[str|None, str|None]

    # parent->chld ipc protocol caps
    _enable_tpts: list[TransportProtocolKey] = field(
        default_factory=lambda: [_def_tpt_proto],
    )

    # registrar info
    _registry_addrs: list[tuple] = []

    # `debug_mode: bool` settings
    _debug_mode: bool = False  # bool
    repl_fixture: bool|Callable = False  # |AbstractContextManager[bool]
    # for `tractor.pause_from_sync()` & `breakpoint()` support
    use_greenback: bool = False

    # infected-`asyncio`-mode: `trio` running as guest.
    _is_infected_aio: bool = False

    def __setattr__(
        self,
        key,
        val,
    ) -> None:
        breakpoint()
        super().__setattr__(key, val)

    def update(
        self,
        from_dict: dict|Struct,
    ) -> None:
        for attr, val in from_dict.items():
            setattr(
                self,
                attr,
                val,
            )


# The "fresh process" defaults — what `_runtime_vars` looks
# like in a just-booted Python process that hasn't yet entered
# `open_root_actor()` nor received a parent `SpawnSpec`. Kept
# as a module-level constant so `get_runtime_vars(clear_values=
# True)` can reset the live dict back to this baseline (see
# `tractor.spawn._subint_forkserver` for the one current caller
# that needs it).
_RUNTIME_VARS_DEFAULTS: dict[str, Any] = {
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
_runtime_vars: dict[str, Any] = dict(_RUNTIME_VARS_DEFAULTS)


def get_runtime_vars(
    as_dict: bool = True,
    clear_values: bool = False,
) -> dict:
    '''
    Deliver a **copy** of the current `Actor`'s "runtime variables".

    By default, for historical impl reasons, this delivers the `dict`
    form, but the `RuntimeVars` struct should be utilized as possible
    for future calls.

    Pure read — **never mutates** the module-level `_runtime_vars`.

    If `clear_values=True`, return a copy of the fresh-process
    defaults (`_RUNTIME_VARS_DEFAULTS`) instead of the live
    dict. Useful in combination with `set_runtime_vars()` to
    reset process-global state back to "cold" — the main caller
    today is the `subint_forkserver` spawn backend's post-fork
    child prelude:

        set_runtime_vars(get_runtime_vars(clear_values=True))

    `os.fork()` inherits the parent's full memory image, so the
    child sees the parent's populated `_runtime_vars` (e.g.
    `_is_root=True`) which would trip the `assert not
    self.enable_modules` gate in `Actor._from_parent()` on the
    subsequent parent→child `SpawnSpec` handshake if left alone.

    '''
    src: dict = (
        _RUNTIME_VARS_DEFAULTS
        if clear_values
        else _runtime_vars
    )
    snapshot: dict = dict(src)
    if as_dict:
        return snapshot
    return RuntimeVars(**snapshot)


def set_runtime_vars(
    rtvars: dict | RuntimeVars,
) -> None:
    '''
    Atomically replace the module-level `_runtime_vars` contents
    with those of `rtvars` (via `.clear()` + `.update()` so
    live references to the same dict object remain valid).

    Accepts either the historical `dict` form or the `RuntimeVars`
    `msgspec.Struct` form (the latter still mostly unused but
    the blessed forward shape — see the struct's definition).

    Paired with `get_runtime_vars()` as the explicit
    write-half of the runtime-vars API — prefer this over
    direct mutation of `_runtime_vars[...]` from new call sites.

    '''
    if isinstance(rtvars, RuntimeVars):
        # `msgspec.Struct` → dict via its declared field set;
        # avoids pulling in `msgspec.structs.asdict` just for
        # this one call path.
        rtvars = {
            field_name: getattr(rtvars, field_name)
            for field_name in rtvars.__struct_fields__
        }
    _runtime_vars.clear()
    _runtime_vars.update(rtvars)


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
        from .._exceptions import NoRuntime

        if last := last_actor():
            msg += (
                f'Apparently the lact active actor was\n'
                f'|_{last}\n'
                f'|_{last.aid.uid}\n'
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
        from .._exceptions import InternalError
        raise InternalError(
            'No IPC context has been allocated for this task yet?\n'
            f'|_{current_task()}\n'
        )
    return ctx



def get_rt_dir(
    subdir: str|Path|None = None,
    appname: str = 'tractor',
) -> Path:
    '''
    Return the user "runtime dir", the file-sys location where most
    userspace apps stick their IPC and cache related system
    util-files.

    On linux we use a `${XDG_RUNTIME_DIR}/tractor/` subdir by
    default, but equivalents are mapped for each platform using
    the lovely `platformdirs` lib.

    '''
    rt_dir: Path = Path(
        platformdirs.user_runtime_dir(
            appname=appname,
        ),
    )

    # Normalize and validate that `subdir` is a relative path
    # without any parent-directory ("..") components, to prevent
    # escaping the runtime directory.
    if subdir:
        subdir_path = (
            subdir
            if isinstance(subdir, Path)
            else Path(subdir)
        )
        if subdir_path.is_absolute():
            raise ValueError(
                f'`subdir` must be a relative path!\n'
                f'{subdir!r}\n'
            )
        if any(part == '..' for part in subdir_path.parts):
            raise ValueError(
                "`subdir` must not contain '..' components!\n"
                f'{subdir!r}\n'
            )

        rt_dir: Path = rt_dir / subdir_path

    if not rt_dir.is_dir():
        rt_dir.mkdir(
            parents=True,
            exist_ok=True,  # avoid `FileExistsError` from conc calls
        )

    return rt_dir


def current_ipc_protos() -> list[str]:
    '''
    Return the list of IPC transport protocol keys currently
    in use by this actor.

    The keys are as declared by `MsgTransport` and `Address`
    concrete-backend sub-types defined throughout `tractor.ipc`.

    '''
    return _runtime_vars['_enable_tpts']
