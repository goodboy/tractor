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
`BaseExceptionGroup` utils and helpers pertaining to
first-class-`trio` from a "historical" perspective, like "loose
exception group" task-nurseries.

'''
from contextlib import (
    asynccontextmanager as acm,
)
from typing import (
    Literal,
    Type,
)

import trio
# from trio._core._concat_tb import (
#     concat_tb,
# )


# XXX NOTE
# taken verbatim from `trio._core._run` except,
# - remove the NONSTRICT_EXCEPTIONGROUP_NOTE deprecation-note
#   guard-check; we know we want an explicit collapse.
# - mask out tb rewriting in collapse case, i don't think it really
#   matters?
#
def collapse_exception_group(
    excgroup: BaseExceptionGroup[BaseException],
) -> BaseException:
    """Recursively collapse any single-exception groups into that single contained
    exception.

    """
    exceptions = list(excgroup.exceptions)
    modified = False
    for i, exc in enumerate(exceptions):
        if isinstance(exc, BaseExceptionGroup):
            new_exc = collapse_exception_group(exc)
            if new_exc is not exc:
                modified = True
                exceptions[i] = new_exc

    if (
        len(exceptions) == 1
        and isinstance(excgroup, BaseExceptionGroup)

        # XXX trio's loose-setting condition..
        # and NONSTRICT_EXCEPTIONGROUP_NOTE in getattr(excgroup, "__notes__", ())
    ):
        # exceptions[0].__traceback__ = concat_tb(
        #     excgroup.__traceback__,
        #     exceptions[0].__traceback__,
        # )
        return exceptions[0]
    elif modified:
        return excgroup.derive(exceptions)
    else:
        return excgroup


def get_collapsed_eg(
    beg: BaseExceptionGroup,

) -> BaseException|None:
    '''
    If the input beg can collapse to a single sub-exception which is
    itself **not** an eg, return it.

    '''
    maybe_exc = collapse_exception_group(beg)
    if maybe_exc is beg:
        return None

    return maybe_exc


@acm
async def collapse_eg(
    hide_tb: bool = True,

    # XXX, for ex. will always show begs containing single taskc
    ignore: set[Type[BaseException]] = {
        # trio.Cancelled,
    },
    add_notes: bool = True,

    bp: bool = False,
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
    except BaseExceptionGroup as _beg:
        beg = _beg

        if bp:
            import tractor
            await tractor.pause(shield=True)

        if (
            (exc := get_collapsed_eg(beg))
            and
            type(exc) not in ignore
        ):

            # TODO? report number of nested groups it was collapsed
            # *from*?
            if add_notes:
                from_group_note: str = (
                    '( ^^^ this exc was collapsed from a group ^^^ )\n'
                )
                if (
                    from_group_note
                    not in
                    getattr(exc, "__notes__", ())
                ):
                    exc.add_note(from_group_note)

            # raise exc
            # ^^ this will leave the orig beg tb above with the
            # "during the handling of <beg> the following.."
            # So, instead do..
            #
            if cause := exc.__cause__:
                raise exc from cause
            else:
                # suppress "during handling of <the beg>"
                # output in tb/console.
                raise exc from None

        # keep original
        raise # beg


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
