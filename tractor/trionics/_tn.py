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
`trio.Nursery` wrappers which we short-hand refer to as
`tn`: "task nursery".

(whereas we refer to `tractor.ActorNursery` as the short-hand `an`)

'''
from __future__ import annotations
from contextlib import (
    asynccontextmanager as acm,
)
from types import ModuleType
from typing import (
    Any,
    AsyncGenerator,
    TYPE_CHECKING,
)

import trio
from tractor.log import get_logger

# from ._beg import (
#     collapse_eg,
# )

if TYPE_CHECKING:
    from tractor import ActorNursery

log = get_logger(__name__)


# ??TODO? is this even a good idea??
# it's an extra LoC to stack `collapse_eg()` vs.
# a new/foreign/bad-std-named very thing wrapper..?
# -[ ] is there a better/simpler name?
# @acm
# async def open_loose_tn() -> trio.Nursery:
#     '''
#     Implements the equivalent of the old style loose eg raising
#     task-nursery from `trio<=0.25.0` ,

#     .. code-block:: python

#         async with trio.open_nursery(
#             strict_exception_groups=False,
#         ) as tn:
#             ...

#     '''
#     async with (
#         collapse_eg(),
#         trio.open_nursery() as tn,
#     ):
#         yield tn


@acm
async def maybe_open_nursery(
    nursery: trio.Nursery|ActorNursery|None = None,
    shield: bool = False,
    lib: ModuleType = trio,
    loose: bool = False,

    **kwargs,  # proxy thru

) -> AsyncGenerator[trio.Nursery, Any]:
    '''
    Create a new nursery if None provided.

    Blocks on exit as expected if no input nursery is provided.

    '''
    if nursery is not None:
        yield nursery
    else:
        async with lib.open_nursery(**kwargs) as tn:
            tn.cancel_scope.shield = shield
            yield tn
