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
from exceptiongroup import BaseExceptionGroup

from ._clustering import open_actor_cluster
from ._context import (
    Context,
    context,
)
from ._streaming import (
    MsgStream,
    stream,
)
from ._discovery import (
    get_arbiter,
    find_actor,
    wait_for_actor,
    query_actor,
)
from ._supervise import open_nursery
from ._state import (
    current_actor,
    is_root_process,
)
from ._exceptions import (
    RemoteActorError,
    ModuleNotExposed,
    ContextCancelled,
)
from ._debug import (
    breakpoint,
    pause,
    pause_from_sync,
    post_mortem,
)
from . import msg
from ._root import (
    run_daemon,
    open_root_actor,
)
from ._ipc import Channel
from ._portal import Portal
from ._runtime import Actor


__all__ = [
    'Actor',
    'BaseExceptionGroup',
    'Channel',
    'Context',
    'ContextCancelled',
    'ModuleNotExposed',
    'MsgStream',
    'Portal',
    'RemoteActorError',
    'breakpoint',
    'context',
    'current_actor',
    'find_actor',
    'query_actor',
    'get_arbiter',
    'is_root_process',
    'msg',
    'open_actor_cluster',
    'open_nursery',
    'open_root_actor',
    'pause',
    'post_mortem',
    'pause_from_sync',
    'query_actor',
    'run_daemon',
    'stream',
    'to_asyncio',
    'wait_for_actor',
]
