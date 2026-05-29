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
import inspect
from types import (
    TracebackType,
)
from typing import (
    Any,
    Awaitable,
    Callable,
    Type,
    TYPE_CHECKING,
)

import trio
from tractor.log import get_logger

log = get_logger()


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


_mask_cases: dict[
    Type[Exception],  # masked exc type
    dict[
        int,  # inner-frame index into `inspect.getinnerframes()`
        # `FrameInfo.function/filename: str`s to match
        dict[str, str],
    ],
] = {
    trio.WouldBlock: {
        # `trio.Lock.acquire()` has a checkpoint inside the
        # `WouldBlock`-no_wait path's handler..
        -5: {  # "5th frame up" from checkpoint
            'filename': 'trio/_sync.py',
            'function': 'acquire',
            # 'lineno': 605,  # matters?
        },
    }
}


def is_expected_masking_case(
    cases: dict,
    exc_ctx: Exception,
    exc_match: BaseException,

) -> bool|inspect.FrameInfo:
    '''
    Determine whether the provided masked exception is from a known
    bug/special/unintentional-`trio`-impl case which we do not wish
    to unmask.

    Return any guilty `inspect.FrameInfo` ow `False`.

    '''
    exc_tb: TracebackType = exc_match.__traceback__
    if cases := _mask_cases.get(type(exc_ctx)):
        inner: list[inspect.FrameInfo] = inspect.getinnerframes(exc_tb)

        # from tractor.devx.debug import mk_pdb
        # mk_pdb().set_trace()
        for iframe, matchon in cases.items():
            try:
                masker_frame: inspect.FrameInfo = inner[iframe]
            except IndexError:
                continue

            for field, in_field in matchon.items():
                val = getattr(
                    masker_frame,
                    field,
                )
                if in_field not in val:
                    break
            else:
                return masker_frame

    return False



# XXX, relevant discussion @ `trio`-core,
# https://github.com/python-trio/trio/issues/455
#
@acm
async def maybe_raise_from_masking_exc(
    unmask_from: (
        BaseException|
        tuple[BaseException]
    ) = (trio.Cancelled,),

    raise_unmasked: bool = True,
    extra_note: str = (
        'This can occurr when,\n'
        '\n'
        ' - a `trio.Nursery/CancelScope` embeds a `finally/except:`-block '
        'which execs an un-shielded checkpoint!'
        #
        # ^TODO? other cases?
    ),

    always_warn_on: tuple[Type[BaseException]] = (
        trio.Cancelled,
    ),

    # don't ever unmask or warn on any masking pair,
    # {<masked-excT-key> -> <masking-excT-value>}
    never_warn_on: dict[
        Type[BaseException],
        Type[BaseException],
    ] = {
        KeyboardInterrupt: trio.Cancelled,
        trio.Cancelled: trio.Cancelled,
    },
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
    if not isinstance(unmask_from, tuple):
        raise ValueError(
            f'Invalid unmask_from = {unmask_from!r}\n'
            f'Must be a `tuple[Type[BaseException]]`.\n'
        )

    from tractor.devx.debug import (
        BoxedMaybeException,
    )
    boxed_maybe_exc = BoxedMaybeException(
        raise_on_exit=raise_unmasked,
    )
    matching: list[BaseException]|None = None
    try:
        yield boxed_maybe_exc
        return
    except BaseException as _bexc:
        bexc = _bexc
        if isinstance(bexc, BaseExceptionGroup):
            matches: ExceptionGroup
            matches, _ = bexc.split(unmask_from)
            if matches:
                matching = matches.exceptions

        elif (
            unmask_from
            and
            type(bexc) in unmask_from
        ):
            matching = [bexc]

    if matching is None:
        raise

    masked: list[tuple[BaseException, BaseException]] = []
    for exc_match in matching:
        if exc_ctx := find_masked_excs(
            maybe_masker=exc_match,
            unmask_from=set(unmask_from),
        ):
            masked.append((
                exc_ctx,
                exc_match,
            ))
            boxed_maybe_exc.value = exc_match
            note: str = (
                f'\n'
                f'^^WARNING^^\n'
                f'the above {type(exc_ctx)!r} was masked by a {type(exc_match)!r}\n'
            )
            if extra_note:
                note += (
                    f'\n'
                    f'{extra_note}\n'
                )

            do_warn: bool = (
                never_warn_on.get(
                    type(exc_ctx)  # masking type
                )
                is not
                type(exc_match)  # masked type
            )

            # Add to masked `exc_ctx`
            if do_warn:
                exc_ctx.add_note(note)

                # don't unmask already known "special" cases..
                if len(masked) < 2:
                    if (
                        _mask_cases
                        and
                        (cases := _mask_cases.get(type(exc_ctx)))
                        and
                        (masker_frame := is_expected_masking_case(
                            cases,
                            exc_ctx,
                            exc_match,
                        ))
                    ):
                        log.warning(
                            f'Ignoring already-known, non-ideal-but-valid '
                            f'masker code @\n'
                            f'{masker_frame}\n'
                            f'\n'
                            f'NOT raising {exc_ctx} from masker {exc_match!r}\n'
                        )
                        raise exc_match

                    # ^?TODO, see above but, possibly unmasking sub-exc
                    # entries if there are > 1
                    # else:
                    #     await pause(shield=True)

                if type(exc_match) in always_warn_on:
                    import traceback
                    trace: list[str] = traceback.format_exception(
                        type(exc_ctx),
                        exc_ctx,
                        exc_ctx.__traceback__
                    )
                    tb_str: str = ''.join(trace)
                    log.warning(tb_str)
                    # XXX, for debug
                    # from tractor import pause
                    # await pause(shield=True)

            if raise_unmasked:
                raise exc_ctx from exc_match


async def start_or_cancel(
    nursery: trio.Nursery,
    async_fn: Callable[..., Awaitable[Any]],
    *args,
    name: object = None,

) -> Any:
    '''
    Like `trio.Nursery.start()` but DON'T mask an out-of-band
    cancellation as a (lossy) startup failure.

    `trio.Nursery.start()` raises a generic
    `RuntimeError("child exited without calling
    task_status.started()")` whenever the started task exits
    BEFORE calling `task_status.started()` — INCLUDING the very
    common case where the child was cancelled out-of-band by an
    *ancestor* cancel-scope erroring/cancelling. In that case the
    original `trio.Cancelled` is swallowed and the caller is left
    with an opaque, root-cause-detached `RuntimeError`.

    This wrapper re-surfaces any ambient (effective, hence
    ancestor-inclusive) cancellation via
    `trio.lowlevel.checkpoint_if_cancelled()` so the real
    `trio.Cancelled` (carrying trio's auto-generated reason which
    points at the true root exc) propagates instead. Only when we
    are NOT under cancellation is the "didn't call `.started()`"
    `RuntimeError` a genuine startup-protocol bug worth surfacing,
    so it's re-raised as-is in that case.

    '''
    try:
        return await nursery.start(
            async_fn,
            *args,
            name=name,
        )
    except RuntimeError as rte:
        if (
            rte.args
            and
            'started' in rte.args[0]
        ):
            # re-raises the in-flight `trio.Cancelled` IFF we're
            # under effective cancellation; else a cheap no-op and
            # we fall through to re-raise the genuine startup RTE.
            await trio.lowlevel.checkpoint_if_cancelled()

        raise

    else:
        raise
