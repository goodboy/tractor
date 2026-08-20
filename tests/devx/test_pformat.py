'''
Unit tests for the `tractor.devx.pformat` render helpers.

'''
from __future__ import annotations

import pytest

from tractor._exceptions import _mk_send_mte
from tractor.devx.pformat import (
    pformat_boxed_tb,
    pformat_caller_frame,
)
from tractor.msg._codec import _def_tractor_codec


@pytest.mark.parametrize(
    'box_tb',
    [True, False],
    ids=['boxed', 'bare'],
)
def test_pformat_caller_frame_renders(box_tb: bool):
    '''
    `pformat_caller_frame()` must render, not raise.

    XXX the `box_tb=True` branch was passing an `indent=''` kwarg
    that `pformat_boxed_tb()` never accepted, so it blew up with
    a `TypeError`. Nothing in the test suite covered it, and the
    only caller is `_mk_send_mte()` — i.e. EVERY send-side
    `MsgTypeError` died while formatting itself, masking the real
    msg-spec violation behind a bogus `TypeError`.

    '''
    report: str = pformat_caller_frame(
        stack_limit=3,
        box_tb=box_tb,
    )
    assert isinstance(report, str)
    assert 'test_pformat_caller_frame_renders' in report


def test_pformat_boxed_tb_rejects_unknown_kwargs():
    '''
    Pin the signature so a future typo'd kwarg fails loudly at the
    call site rather than only when some rare error path runs.

    '''
    assert pformat_boxed_tb(tb_str='doggy\n')

    with pytest.raises(TypeError):
        pformat_boxed_tb(
            tb_str='doggy\n',
            indent='',
        )


def test_send_mte_default_message_renders():
    '''
    The default send-side `MsgTypeError` must remain printable.

    Once `pformat_caller_frame()` stopped failing first, this path
    exposed two more formatter errors: `MsgCodec.msg_spec_str` passed
    a type union where `pformat_msgspec()` requires a codec/decoder,
    then `_mk_send_mte()` wrapped its message in a one-element tuple.

    Construct the error without an override message to execute that
    complete default path. Requiring a `str` message with the bad
    value and valid spec, then rendering the exception, proves the
    original IPC violation survives every formatter layer.

    '''
    bad_msg: dict[str, bool] = {'bad': True}
    mte = _mk_send_mte(
        msg=bad_msg,
        codec=_def_tractor_codec,
    )

    assert isinstance(mte.message, str)
    assert f'invalid msg -> {bad_msg}' in mte.message
    assert 'Valid IPC msgs are:' in mte.message

    report: str = repr(mte)
    assert 'MsgTypeError' in report
    assert f'invalid msg -> {bad_msg}' in report
