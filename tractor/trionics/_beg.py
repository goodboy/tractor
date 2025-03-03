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
`BaseExceptionGroup` related utils and helpers pertaining to
first-class-`trio` from a historical perspective B)

'''
from contextlib import (
    # bontextmanager as cm,
    asynccontextmanager as acm,
)


def maybe_collapse_eg(
    beg: BaseExceptionGroup,
) -> BaseException:
    '''
    If the input beg can collapse to a single non-eg sub-exception,
    return it instead.

    '''
    if len(excs := beg.exceptions) == 1:
        return excs[0]

    return beg


@acm
async def collapse_eg():
    '''
    If `BaseExceptionGroup` raised in the body scope is
    "collapse-able" (in the same way that
    `trio.open_nursery(strict_exception_groups=False)` works) then
    only raise the lone emedded non-eg in in place.

    '''
    try:
        yield
    except* BaseException as beg:
        if (
            exc := maybe_collapse_eg(beg)
        ) is not beg:
            raise exc

        raise beg
