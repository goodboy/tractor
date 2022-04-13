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
tractor: structured concurrent "actors".

"""
from trio import MultiError

from ._clustering import open_actor_cluster
from ._ipc import Channel
from ._streaming import (
    Context,
    ReceiveMsgStream,
    MsgStream,
    stream,
    context,
)
from ._discovery import (
    get_arbiter,
    find_actor,
    wait_for_actor,
    query_actor,
)
from ._supervise import open_nursery
from ._state import current_actor, is_root_process
from ._exceptions import (
    RemoteActorError,
    ModuleNotExposed,
    ContextCancelled,
)
from ._debug import breakpoint, post_mortem
from . import msg
from ._root import run, run_daemon, open_root_actor
from ._portal import Portal


__all__ = [
    'Channel',
    'Context',
    'ContextCancelled',
    'ModuleNotExposed',
    'MsgStream',
    'MultiError',
    'Portal',
    'ReceiveMsgStream',
    'RemoteActorError',
    'breakpoint',
    'context',
    'current_actor',
    'find_actor',
    'get_arbiter',
    'is_root_process',
    'msg',
    'open_actor_cluster',
    'open_nursery',
    'open_root_actor',
    'post_mortem',
    'query_actor',
    'run',
    'run_daemon',
    'stream',
    'to_asyncio',
    'wait_for_actor',
]
