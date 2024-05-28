'''
Audit sub-sys APIs from `.msg._ops`
mostly for ensuring correct `contextvars`
related settings around IPC contexts.

'''
from contextlib import (
    asynccontextmanager as acm,
)
from contextvars import (
    Context,
)

from msgspec import (
    Struct,
)
import pytest
import trio

import tractor
from tractor import (
    MsgTypeError,
    current_ipc_ctx,
    Portal,
)
from tractor.msg import (
    _ops as msgops,
    Return,
)
from tractor.msg import (
    _codec,
)
from tractor.msg.types import (
    log,
)


class PldMsg(Struct):
    field: str


maybe_msg_spec = PldMsg|None


@acm
async def maybe_expect_raises(
    raises: BaseException|None = None,
    ensure_in_message: list[str]|None = None,
    reraise: bool = False,
    timeout: int = 3,
) -> None:
    '''
    Async wrapper for ensuring errors propagate from the inner scope.

    '''
    with trio.fail_after(timeout):
        try:
            yield
        except BaseException as _inner_err:
            inner_err = _inner_err
            # wasn't-expected to error..
            if raises is None:
                raise

            else:
                assert type(inner_err) is raises

                # maybe check for error txt content
                if ensure_in_message:
                    part: str
                    for part in ensure_in_message:
                        for i, arg in enumerate(inner_err.args):
                            if part in arg:
                                break
                        # if part never matches an arg, then we're
                        # missing a match.
                        else:
                            raise ValueError(
                                'Failed to find error message content?\n\n'
                                f'expected: {ensure_in_message!r}\n'
                                f'part: {part!r}\n\n'
                                f'{inner_err.args}'
                        )

                if reraise:
                    raise inner_err

        else:
            if raises:
                raise RuntimeError(
                    f'Expected a {raises.__name__!r} to be raised?'
                )


@tractor.context
async def child(
    ctx: Context,
    started_value: int|PldMsg|None,
    return_value: str|None,
    validate_pld_spec: bool,
    raise_on_started_mte: bool = True,

) -> None:
    '''
    Call ``Context.started()`` more then once (an error).

    '''
    expect_started_mte: bool = started_value == 10

    # sanaity check that child RPC context is the current one
    curr_ctx: Context = current_ipc_ctx()
    assert ctx is curr_ctx

    rx: msgops.PldRx = ctx._pld_rx
    orig_pldec: _codec.MsgDec = rx.pld_dec
    # senity that default pld-spec should be set
    assert (
        rx.pld_dec
        is
        msgops._def_any_pldec
    )

    try:
        with msgops.limit_plds(
            spec=maybe_msg_spec,
        ) as pldec:
            # sanity on `MsgDec` state
            assert rx.pld_dec is pldec
            assert pldec.spec is maybe_msg_spec

            # 2 cases: hdndle send-side and recv-only validation
            # - when `raise_on_started_mte == True`, send validate
            # - else, parent-recv-side only validation
            try:
                await ctx.started(
                    value=started_value,
                    validate_pld_spec=validate_pld_spec,
                )

            except MsgTypeError:
                log.exception('started()` raised an MTE!\n')
                if not expect_started_mte:
                    raise RuntimeError(
                        'Child-ctx-task SHOULD NOT HAVE raised an MTE for\n\n'
                        f'{started_value!r}\n'
                    )

                # propagate to parent?
                if raise_on_started_mte:
                    raise
            else:
                if expect_started_mte:
                    raise RuntimeError(
                        'Child-ctx-task SHOULD HAVE raised an MTE for\n\n'
                        f'{started_value!r}\n'
                    )

            # XXX should always fail on recv side since we can't
            # really do much else beside terminate and relay the
            # msg-type-error from this RPC task ;)
            return return_value

    finally:
        # sanity on `limit_plds()` reversion
        assert (
            rx.pld_dec
            is
            msgops._def_any_pldec
        )
        log.runtime(
            'Reverted to previous pld-spec\n\n'
            f'{orig_pldec}\n'
        )


@pytest.mark.parametrize(
    'return_value',
    [
        None,
        'yo',
    ],
    ids=[
        'return[invalid-"yo"]',
        'return[valid-None]',
    ],
)
@pytest.mark.parametrize(
    'started_value',
    [
        10,
        PldMsg(field='yo'),
    ],
    ids=[
        'Started[invalid-10]',
        'Started[valid-PldMsg]',
    ],
)
@pytest.mark.parametrize(
    'pld_check_started_value',
    [
        True,
        False,
    ],
    ids=[
        'check-started-pld',
        'no-started-pld-validate',
    ],
)
def test_basic_payload_spec(
    debug_mode: bool,
    loglevel: str,
    return_value: str|None,
    started_value: int|PldMsg,
    pld_check_started_value: bool,
):
    '''
    Validate the most basic `PldRx` msg-type-spec semantics around
    a IPC `Context` endpoint start, started-sync, and final return
    value depending on set payload types and the currently applied
    pld-spec.

    '''
    invalid_return: bool = return_value == 'yo'
    invalid_started: bool = started_value == 10

    async def main():
        async with tractor.open_nursery(
            debug_mode=debug_mode,
            loglevel=loglevel,
        ) as an:
            p: Portal = await an.start_actor(
                'child',
                enable_modules=[__name__],
            )

            # since not opened yet.
            assert current_ipc_ctx() is None

            if invalid_started:
                msg_type_str: str = 'Started'
                bad_value_str: str = '10'
            elif invalid_return:
                msg_type_str: str = 'Return'
                bad_value_str: str = "'yo'"
            else:
                # XXX but should never be used below then..
                msg_type_str: str = ''
                bad_value_str: str = ''

            async with (
                maybe_expect_raises(
                    raises=MsgTypeError if (
                        invalid_return
                        or
                        invalid_started
                    ) else None,
                    ensure_in_message=[
                        f"invalid `{msg_type_str}` payload",
                        f"value: `{bad_value_str}` does not match type-spec: `{msg_type_str}.pld: PldMsg|NoneType`",
                    ],
                ),
                p.open_context(
                    child,
                    return_value=return_value,
                    started_value=started_value,
                    pld_spec=maybe_msg_spec,
                    validate_pld_spec=pld_check_started_value,
                ) as (ctx, first),
            ):
                # now opened with 'child' sub
                assert current_ipc_ctx() is ctx

                assert type(first) is PldMsg
                assert first.field == 'yo'

                try:
                    assert (await ctx.result()) is None
                except MsgTypeError as mte:
                    if not invalid_return:
                        raise

                    else:  # expected this invalid `Return.pld`
                        assert mte.cid == ctx.cid

                        # verify expected remote mte deats
                        await tractor.pause()
                        assert ctx._remote_error is mte
                        assert mte.expected_msg_type is Return

            await p.cancel_actor()

    trio.run(main)
