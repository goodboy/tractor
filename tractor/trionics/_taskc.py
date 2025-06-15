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
`trio.Task` cancellation helpers, extensions and "holsters".

'''
from __future__ import annotations
from contextlib import (
    asynccontextmanager as acm,
)
from typing import TYPE_CHECKING

import trio
from tractor.log import get_logger

log = get_logger(__name__)


if TYPE_CHECKING:
    from tractor.devx.debug import BoxedMaybeException


def find_masked_excs(
    maybe_masker: BaseException,
    unmask_from: set[BaseException],
) -> BaseException|None:
    ''''
    Deliver any `maybe_masker.__context__` provided
    it a declared masking exc-type entry in `unmask_from`.

    '''
    if (
        type(maybe_masker) in unmask_from
        and
        (exc_ctx := maybe_masker.__context__)

        # TODO? what about any cases where
        # they could be the same type but not same instance?
        # |_i.e. a cancel masking a cancel ??
        # or (
        #     exc_ctx is not maybe_masker
        # )
    ):
        return exc_ctx

    return None


@acm
async def maybe_raise_from_masking_exc(
    tn: trio.Nursery|None = None,
    unmask_from: (
        BaseException|
        tuple[BaseException]
    ) = (trio.Cancelled,),

    raise_unmasked: bool = True,
    extra_note: str = (
        'This can occurr when,\n'
        ' - a `trio.Nursery` scope embeds a `finally:`-block '
        'which executes a checkpoint!'
        #
        # ^TODO? other cases?
    ),

    always_warn_on: tuple[BaseException] = (
        trio.Cancelled,
    ),
    # ^XXX, special case(s) where we warn-log bc likely
    # there will be no operational diff since the exc
    # is always expected to be consumed.
) -> BoxedMaybeException:
    '''
    Maybe un-mask and re-raise exception(s) suppressed by a known
    error-used-as-signal type (cough namely `trio.Cancelled`).

    Though this unmasker targets cancelleds, it can be used more
    generally to capture and unwrap masked excs detected as
    `.__context__` values which were suppressed by any error type
    passed in `unmask_from`.

    -------------
    STILL-TODO ??
    -------------
    -[ ] support for egs which have multiple masked entries in
        `maybe_eg.exceptions`, in which case we should unmask the
        individual sub-excs but maintain the eg-parent's form right?

    '''
    from tractor.devx.debug import (
        BoxedMaybeException,
        pause,
    )
    boxed_maybe_exc = BoxedMaybeException(
        raise_on_exit=raise_unmasked,
    )
    matching: list[BaseException]|None = None
    maybe_eg: ExceptionGroup|None
    maybe_eg: ExceptionGroup|None

    if tn:
        try:  # handle egs
            yield boxed_maybe_exc
            return
        except* unmask_from as _maybe_eg:
            maybe_eg = _maybe_eg
            matches: ExceptionGroup
            matches, _ = maybe_eg.split(
                unmask_from
            )
            if not matches:
                raise

            matching: list[BaseException] = matches.exceptions
    else:
        try:  # handle non-egs
            yield boxed_maybe_exc
            return
        except unmask_from as _maybe_exc:
            maybe_exc = _maybe_exc
            matching: list[BaseException] = [
                maybe_exc
            ]

        # XXX, only unmask-ed for debuggin!
        # TODO, remove eventually..
        except BaseException as _berr:
            berr = _berr
            await pause(shield=True)
            raise berr

    if matching is None:
        raise

    masked: list[tuple[BaseException, BaseException]] = []
    for exc_match in matching:

        if exc_ctx := find_masked_excs(
            maybe_masker=exc_match,
            unmask_from={unmask_from},
        ):
            masked.append((exc_ctx, exc_match))
            boxed_maybe_exc.value = exc_match
            note: str = (
                f'\n'
                f'^^WARNING^^ the above {exc_ctx!r} was masked by a {unmask_from!r}\n'
            )
            if extra_note:
                note += (
                    f'\n'
                    f'{extra_note}\n'
                )
            exc_ctx.add_note(note)

            if type(exc_match) in always_warn_on:
                log.warning(note)

            # await tractor.pause(shield=True)
            if raise_unmasked:

                if len(masked) < 2:
                    raise exc_ctx from exc_match
                else:
                    # ?TODO, see above but, possibly unmasking sub-exc
                    # entries if there are > 1
                    await pause(shield=True)
    else:
        raise
