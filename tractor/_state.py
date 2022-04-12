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
from typing import Optional, Dict, Any
from collections.abc import Mapping

import trio

from ._exceptions import NoRuntime


_current_actor: Optional['Actor'] = None  # type: ignore # noqa
_runtime_vars: Dict[str, Any] = {
    '_debug_mode': False,
    '_is_root': False,
    '_root_mailbox': (None, None)
}


def current_actor(err_on_no_runtime: bool = True) -> 'Actor':  # type: ignore # noqa
    """Get the process-local actor instance.
    """
    if _current_actor is None and err_on_no_runtime:
        raise NoRuntime("No local actor has been initialized yet")

    return _current_actor


_conc_name_getters = {
    'task': trio.lowlevel.current_task,
    'actor': current_actor
}


class ActorContextInfo(Mapping):
    "Dyanmic lookup for local actor and task names"
    _context_keys = ('task', 'actor')

    def __len__(self):
        return len(self._context_keys)

    def __iter__(self):
        return iter(self._context_keys)

    def __getitem__(self, key: str) -> str:
        try:
            return _conc_name_getters[key]().name  # type: ignore
        except RuntimeError:
            # no local actor/task context initialized yet
            return f'no {key} context'


def is_main_process() -> bool:
    """Bool determining if this actor is running in the top-most process.
    """
    import multiprocessing as mp
    return mp.current_process().name == 'MainProcess'


def debug_mode() -> bool:
    """Bool determining if "debug mode" is on which enables
    remote subactor pdb entry on crashes.
    """
    return bool(_runtime_vars['_debug_mode'])


def is_root_process() -> bool:
    return _runtime_vars['_is_root']
