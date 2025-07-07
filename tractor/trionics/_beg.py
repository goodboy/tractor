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
    asynccontextmanager as acm,
)
from typing import (
    Literal,
)

import trio


def maybe_collapse_eg(
    beg: BaseExceptionGroup,
) -> BaseException|bool:
    '''
    If the input beg can collapse to a single non-eg sub-exception,
    return it instead.

    '''
    if len(excs := beg.exceptions) == 1:
        return excs[0]

    return False


@acm
async def collapse_eg(
    hide_tb: bool = True,
):
    '''
    If `BaseExceptionGroup` raised in the body scope is
    "collapse-able" (in the same way that
    `trio.open_nursery(strict_exception_groups=False)` works) then
    only raise the lone emedded non-eg in in place.

    '''
    __tracebackhide__: bool = hide_tb
    try:
        yield
    except* BaseException as beg:
        if (
            exc := maybe_collapse_eg(beg)
        ):
            if cause := exc.__cause__:
                raise exc from cause

            raise exc

        raise beg


def is_multi_cancelled(
    beg: BaseException|BaseExceptionGroup,

    ignore_nested: set[BaseException] = set(),

) -> Literal[False]|BaseExceptionGroup:
    '''
    Predicate to determine if an `BaseExceptionGroup` only contains
    some (maybe nested) set of sub-grouped exceptions (like only
    `trio.Cancelled`s which get swallowed silently by default) and is
    thus the result of "gracefully cancelling" a collection of
    sub-tasks (or other conc primitives) and receiving a "cancelled
    ACK" from each after termination.

    Docs:
    ----
    - https://docs.python.org/3/library/exceptions.html#exception-groups
    - https://docs.python.org/3/library/exceptions.html#BaseExceptionGroup.subgroup

    '''

    if (
        not ignore_nested
        or
        trio.Cancelled not in ignore_nested
        # XXX always count-in `trio`'s native signal
    ):
        ignore_nested.update({trio.Cancelled})

    if isinstance(beg, BaseExceptionGroup):
        # https://docs.python.org/3/library/exceptions.html#BaseExceptionGroup.subgroup
        # |_ "The condition can be an exception type or tuple of
        #   exception types, in which case each exception is checked
        #   for a match using the same check that is used in an
        #   except clause. The condition can also be a callable
        #   (other than a type object) that accepts an exception as
        #   its single argument and returns true for the exceptions
        #   that should be in the subgroup."
        matched_exc: BaseExceptionGroup|None = beg.subgroup(
            tuple(ignore_nested),

            # ??TODO, complain about why not allowed to use
            # named arg style calling???
            # XD .. wtf?
            # condition=tuple(ignore_nested),
        )
        if matched_exc is not None:
            return matched_exc

    # NOTE, IFF no excs types match (throughout the error-tree)
    # -> return `False`, OW return the matched sub-eg.
    #
    # IOW, for the inverse of ^ for the purpose of
    # maybe-enter-REPL--logic: "only debug when the err-tree contains
    # at least one exc-type NOT in `ignore_nested`" ; i.e. the case where
    # we fallthrough and return `False` here.
    return False
