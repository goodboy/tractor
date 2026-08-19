'''
Unit tests for the `tractor.devx.pformat` render helpers.

'''
from __future__ import annotations

import pytest

from tractor.devx.pformat import (
    pformat_boxed_tb,
    pformat_caller_frame,
)


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
