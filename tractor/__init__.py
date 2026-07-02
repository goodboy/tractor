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
tractor: structured concurrent ``trio``-"actors".

"""

from ._clustering import (
    open_actor_cluster as open_actor_cluster,
)
from ._context import (
    Context as Context,  # the type
    context as context,  # a func-decorator
)
from ._streaming import (
    MsgStream as MsgStream,
    stream as stream,
)
from .discovery._api import (
    get_registry as get_registry,
    find_actor as find_actor,
    wait_for_actor as wait_for_actor,
    query_actor as query_actor,
)
from .runtime._supervise import (
    open_nursery as open_nursery,
    ActorNursery as ActorNursery,
)
from .runtime._state import (
    RuntimeVars as RuntimeVars,
    current_actor as current_actor,
    current_ipc_ctx as current_ipc_ctx,
    debug_mode as debug_mode,
    get_runtime_vars as get_runtime_vars,
    is_root_process as is_root_process,
)
from ._exceptions import (
    ContextCancelled as ContextCancelled,
    ModuleNotExposed as ModuleNotExposed,
    MsgTypeError as MsgTypeError,
    RemoteActorError as RemoteActorError,
    TransportClosed as TransportClosed,
)
from .devx import (
    breakpoint as breakpoint,
    pause as pause,
    pause_from_sync as pause_from_sync,
    post_mortem as post_mortem,
)
from . import msg as msg
from ._root import (
    run_daemon as run_daemon,
    open_root_actor as open_root_actor,
)
from .ipc import Channel as Channel
from .runtime._portal import Portal as Portal
from .runtime._runtime import Actor as Actor
from .discovery._registry import (
    Registrar as Registrar,
    Arbiter as Arbiter,
)
# from . import hilevel as hilevel


def __getattr__(name: str):
    '''
    PEP 562 lazy sub-module loading, presently only for
    `.to_asyncio` which (transitively) imports `asyncio`
    itself: a non-trivial multi-ms chunk of the eager
    `import tractor` cost (gh #470) unneeded by
    `trio`-only apps.

    Any `tractor.to_asyncio.<attr>` access (or a
    `from tractor import to_asyncio`) still works, the
    sub-mod is simply imported on first-access instead
    of at pkg-import time.

    '''
    if name == 'to_asyncio':
        from importlib import import_module
        return import_module('.to_asyncio', __name__)

    raise AttributeError(
        f'module {__name__!r} has no attribute {name!r}'
    )
