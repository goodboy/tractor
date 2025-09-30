'''
Audit sub-sys APIs from `.msg._ops`
mostly for ensuring correct `contextvars`
related settings around IPC contexts.

'''
from contextlib import (
    asynccontextmanager as acm,
)
import sys
import types
from typing import (
    Any,
    Union,
    Type,
)

import msgspec
from msgspec import (
    Struct,
)
import pytest
import trio

import tractor
from tractor import (
    Context,
    MsgTypeError,
    current_ipc_ctx,
    Portal,
)
from tractor.msg import (
    _codec,
    _ops as msgops,
    Return,
    _exts,
)
from tractor.msg.types import (
    log,
)


class PldMsg(
    Struct,

    # TODO: with multiple structs in-spec we need to tag them!
    # -[ ] offer a built-in `PldMsg` type to inherit from which takes
    #      case of these details?
    #
    # https://jcristharif.com/msgspec/structs.html#tagged-unions
    tag=True,
    tag_field='msg_type',
):
    field: str


class Msg1(PldMsg):
    field: str


class Msg2(PldMsg):
    field: int


class AnyFieldMsg(PldMsg):
    field: Any


@acm
async def maybe_expect_raises(
    raises: BaseException|None = None,
    ensure_in_message: list[str]|None = None,
    post_mortem: bool = False,
    timeout: int = 3,
) -> None:
    '''
    Async wrapper for ensuring errors propagate from the inner scope.

    '''
    if tractor._state.debug_mode():
        timeout += 999

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
                    err_repr: str = repr(inner_err)
                    for part in ensure_in_message:
                        for i, arg in enumerate(inner_err.args):
                            if part in err_repr:
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

                if post_mortem:
                    await tractor.post_mortem()

        else:
            if raises:
                raise RuntimeError(
                    f'Expected a {raises.__name__!r} to be raised?'
                )


# NOTE, this decorator is applied dynamically by both the root and
# 'sub' actor such that we can dynamically apply various cases from
# a parametrized test.
#
# maybe_msg_spec = PldMsg|None
#
# @tractor.context(
#     pld_spec=maybe_msg_spec,
# )
async def child(
    ctx: Context,
    started_value: int|PldMsg|None,
    return_value: str|None,
    validate_pld_spec: bool,
    raise_on_started_mte: bool = True,

    pack_any_field: bool = False,

) -> None:
    '''
    Call ``Context.started()`` more then once (an error).

    '''
    # sanaity check that child RPC context is the current one
    curr_ctx: Context = current_ipc_ctx()
    assert ctx is curr_ctx

    rx: msgops.PldRx = ctx._pld_rx
    curr_pldec: _codec.MsgDec = rx.pld_dec


    ctx_meta: dict = getattr(
        child,
        '_tractor_context_meta',
        None,
    )
    if ctx_meta:
        assert (
            ctx_meta['pld_spec']
            is
            curr_pldec.spec
            is
            curr_pldec.pld_spec
        )

    pld_types: set[Type] = _codec.unpack_spec_types(
        curr_pldec.pld_spec,
    )
    if (
        AnyFieldMsg in pld_types
        and
        pack_any_field
    ):
        started_value = AnyFieldMsg(field=started_value)

    expect_started_mte: bool = (
        started_value == 10
        and
        not pack_any_field
    )

    # 2 cases: hdndle send-side and recv-only validation
    # - when `raise_on_started_mte == True`, send validate
    # - else, parent-recv-side only validation
    mte: MsgTypeError|None = None
    try:
        await ctx.started(
            value=started_value,
            validate_pld_spec=validate_pld_spec,
        )

    except MsgTypeError as _mte:
        mte = _mte
        log.exception('started()` raised an MTE!\n')
        if not expect_started_mte:
            raise RuntimeError(
                'Child-ctx-task SHOULD NOT HAVE raised an MTE for\n\n'
                f'{started_value!r}\n'
            )

        boxed_div: str = '------ - ------'
        assert boxed_div not in mte._message
        assert boxed_div not in mte.tb_str
        assert boxed_div not in repr(mte)
        assert boxed_div not in str(mte)
        mte_repr: str = repr(mte)
        for line in mte.message.splitlines():
            assert line in mte_repr

        # since this is a *local error* there should be no
        # boxed traceback content!
        assert not mte.tb_str

        # propagate to parent?
        if raise_on_started_mte:
            raise

    # no-send-side-error fallthrough
    if (
        validate_pld_spec
        and
        expect_started_mte
    ):
        raise RuntimeError(
            'Child-ctx-task SHOULD HAVE raised an MTE for\n\n'
            f'{started_value!r}\n'
        )

    assert (
        not expect_started_mte
        or
        not validate_pld_spec
    )

    # if wait_for_parent_to_cancel:
    #     ...
    #
    # ^-TODO-^ logic for diff validation policies on each side:
    #
    # -[ ] ensure that if we don't validate on the send
    #   side, that we are eventually error-cancelled by our
    #   parent due to the bad `Started` payload!
    # -[ ] the boxed error should be srced from the parent's
    #   runtime NOT ours!
    # -[ ] we should still error on bad `return_value`s
    #   despite the parent not yet error-cancelling us?
    #   |_ how do we want the parent side to look in that
    #     case?
    #     -[ ] maybe the equiv of "during handling of the
    #       above error another occurred" for the case where
    #       the parent sends a MTE to this child and while
    #       waiting for the child to terminate it gets back
    #       the MTE for this case?
    #

    # XXX should always fail on recv side since we can't
    # really do much else beside terminate and relay the
    # msg-type-error from this RPC task ;)
    return return_value

def decorate_child_ep(
    pld_spec: Union[Type],
) -> types.ModuleType:
    '''
    Apply parametrized pld_spec to ctx ep like,

        @tractor.context(
            pld_spec=maybe_msg_spec,
        )(child)

    '''
    this_mod = sys.modules[__name__]
    global child  # a mod-fn defined above
    assert this_mod.child is child
    this_mod.child = tractor.context(
        pld_spec=pld_spec,
    )(child)
    return this_mod


@tractor.context
async def set_chld_pldspec(
    ctx: tractor.Context,
    pld_spec_strs: list[str],
):
    '''
    Dynamically apply the `@context(pld_spec=pld_spec)` deco to the
    current actor's in-mem instance of this test module.

    Allows dynamically applying the "payload-spec" in both a parent
    and child actor after spawn.

    '''
    this_mod = sys.modules[__name__]
    pld_spec: list[str] = _exts.dec_type_union(
        pld_spec_strs,
        mods=[
            this_mod,
            msgspec.inspect,
        ],
    )
    decorate_child_ep(pld_spec)
    await ctx.started()
    await trio.sleep_forever()


@pytest.mark.parametrize(
    'return_value',
    [
        'yo',
        None,
        Msg2(field=10),
        AnyFieldMsg(field='yo'),
    ],
    ids=[
        'return[invalid-"yo"]',
        'return[maybe-valid-None]',
        'return[maybe-valid-Msg2]',
        'return[maybe-valid-any-packed-yo]',
    ],
)
@pytest.mark.parametrize(
    'started_value',
    [
        10,
        PldMsg(field='yo'),
        Msg1(field='yo'),
        AnyFieldMsg(field=10),
    ],
    ids=[
        'Started[invalid-10]',
        'Started[maybe-valid-PldMsg]',
        'Started[maybe-valid-Msg1]',
        'Started[maybe-valid-any-packed-10]',
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
@pytest.mark.parametrize(
    'pld_spec',
    [
        PldMsg|None,

        # demo how to have strict msgs alongside all other supported
        # py-types by embedding the any-types inside a shuttle msg.
        Msg1|Msg2|AnyFieldMsg,

        # XXX, will never work since Struct overrides dict.
        # https://jcristharif.com/msgspec/usage.html#typed-decoding
        # Msg1|Msg2|msgspec.inspect.AnyType,
    ],
    ids=[
        'maybe_PldMsg_spec',
        'Msg1_or_Msg2_or_AnyFieldMsg_spec',
    ]
)
def test_basic_payload_spec(
    debug_mode: bool,
    loglevel: str,
    return_value: str|None,
    started_value: int|PldMsg,
    pld_check_started_value: bool,
    pld_spec: Union[Type],
):
    '''
    Validate the most basic `PldRx` msg-type-spec semantics around
    a IPC `Context` endpoint start, started-sync, and final return
    value depending on set payload types and the currently applied
    pld-spec.

    '''
    pld_types: set[Type] = _codec.unpack_spec_types(pld_spec)
    invalid_return: bool = (
        return_value == 'yo'
    )
    invalid_started: bool = (
        started_value == 10
    )

    # dynamically apply ep's pld-spec in 'root'.
    decorate_child_ep(pld_spec)
    assert (
        child._tractor_context_meta['pld_spec'] == pld_spec
    )
    pld_spec_strs: list[str] = _exts.enc_type_union(
        pld_spec,
    )
    assert len(pld_types) > 1

    async def main():
        nonlocal pld_spec

        async with tractor.open_nursery(
            debug_mode=debug_mode,
            loglevel=loglevel,
        ) as an:
            p: Portal = await an.start_actor(
                'sub',
                enable_modules=[__name__],
            )

            # since not opened yet.
            assert current_ipc_ctx() is None

            if invalid_started:
                msg_type_str: str = 'Started'
                bad_value: int = 10

            elif invalid_return:
                msg_type_str: str = 'Return'
                bad_value: str = 'yo'

            else:
                # XXX but should never be used below then..
                msg_type_str: str = ''
                bad_value: str = ''

            maybe_mte: MsgTypeError|None = None
            should_raise: Exception|None = (
                MsgTypeError if (
                    invalid_return
                    or
                    invalid_started
                ) else None
            )

            async with (
                maybe_expect_raises(
                    raises=should_raise,
                    ensure_in_message=[
                        f"invalid `{msg_type_str}` msg payload",
                        f'{bad_value}',
                        f'has type {type(bad_value)!r}',
                        'not match type-spec',
                        f'`{msg_type_str}.pld: PldMsg|NoneType`',
                    ],
                    # only for debug
                    # post_mortem=True,
                ),
                p.open_context(
                    set_chld_pldspec,
                    pld_spec_strs=pld_spec_strs,
                ) as (deco_ctx, _),

                p.open_context(
                    child,
                    return_value=return_value,
                    started_value=started_value,
                    validate_pld_spec=pld_check_started_value,
                ) as (ctx, first),
            ):
                # now opened with 'child' sub
                assert current_ipc_ctx() is ctx

                # assert type(first) is PldMsg
                assert isinstance(first, PldMsg)
                assert first.field == 'yo'

                try:
                    res: None|PldMsg = await ctx.result(hide_tb=False)
                    assert res == return_value
                    if res is None:
                        await tractor.pause()
                    if isinstance(res, PldMsg):
                        assert res.field == 10

                except MsgTypeError as mte:
                    maybe_mte = mte
                    if not invalid_return:
                        raise

                    # expected this invalid `Return.pld` so audit
                    # the error state + meta-data
                    assert mte.expected_msg_type is Return
                    assert mte.cid == ctx.cid
                    mte_repr: str = repr(mte)
                    for line in mte.message.splitlines():
                        assert line in mte_repr

                    assert mte.tb_str
                    # await tractor.pause(shield=True)

                    # verify expected remote mte deats
                    assert ctx._local_error is None
                    assert (
                        mte is
                        ctx._remote_error is
                        ctx.maybe_error is
                        ctx.outcome
                    )

                if should_raise is None:
                    await deco_ctx.cancel()

            if should_raise is None:
                assert maybe_mte is None

            await p.cancel_actor()

    trio.run(main)
