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
(Originally) Helpers pulled verbatim from ``multiprocessing.spawn``
to aid with "fixing up" the ``__main__`` module in subprocesses.

Now just delegates directly to appropriate `mp.spawn` fns.

Note
----
These helpers are needed for any spawing backend that doesn't already
handle this. For example it's needed when using our
`start_method='trio' backend but not when we're already using
a ``multiprocessing`` backend such as 'mp_spawn', 'mp_forkserver'.

?TODO?
- what will be required for an eventual subint backend?

The helpers imported from `mp.spawn` provide the stdlib's
spawn/forkserver bootstrap that rebuilds the parent's `__main__` in
a fresh child interpreter. In particular, we capture enough info to
later replay the parent's main module as `__mp_main__` (or by path)
in the child process.

See:
https://docs.python.org/3/library/multiprocessing.html#the-spawn-and-forkserver-start-methods

'''
import multiprocessing as mp
from multiprocessing.spawn import (
    _fixup_main_from_name as _fixup_main_from_name,
    _fixup_main_from_path as _fixup_main_from_path,
    get_preparation_data,
)
from typing import NotRequired
from typing import TypedDict


class ParentMainData(TypedDict):
    init_main_from_name: NotRequired[str]
    init_main_from_path: NotRequired[str]


def _mp_figure_out_main(
    inherit_parent_main: bool = True,
) -> ParentMainData:
    '''
    Delegate to `multiprocessing.spawn.get_preparation_data()`
    when `inherit_parent_main=True`.

    Retrieve parent (actor) proc's  `__main__` module data.

    '''
    if not inherit_parent_main:
        return {}

    d: ParentMainData
    proc: mp.Process = mp.current_process()
    d: dict = get_preparation_data(
        name=proc.name,
    )
    # XXX, unserializable (and uneeded by us) by default
    # see `mp.spawn.get_preparation_data()` impl deats.
    d.pop('authkey')
    return d
