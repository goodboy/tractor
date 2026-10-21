'''
Unit tests for the `tractor.devx.pformat` render helpers.

'''
from __future__ import annotations

import pytest
import trio

from tractor._exceptions import _mk_send_mte
from tractor.log import pformat_task_uid as log_task_uid
from tractor.devx.pformat import (
    pformat_boxed_tb,
    pformat_caller_frame,
    pformat_task,
    pformat_task_uid,
)
from tractor.msg._codec import _def_tractor_codec


def test_pformat_task_selects_another_tasks_identity() -> None:
    '''
    Describe the selected task rather than the diagnostic caller.

    The final-Return regression needs to snapshot an endpoint and its
    nursery owner from another task. Hold a real child in an active
    nursery and inspect it from its parent, checking both identities
    and public nursery relationships without requiring an actor.
    Preserve the log header's decimal head/tail ID convention while
    allowing that existing helper to select an explicit task too.

    '''
    async def main() -> None:
        '''
        Keep the inspected child alive through all report assertions.

        '''
        async def child(
            task_status: trio.TaskStatus[trio.lowlevel.Task],
        ) -> None:
            '''
            Publish the real task before waiting for cancellation.

            '''
            task_status.started(trio.lowlevel.current_task())
            await trio.sleep_forever()

        owner: trio.lowlevel.Task = trio.lowlevel.current_task()
        owner_id: str = str(id(owner))
        assert pformat_task_uid() == f'{owner.name}[{owner_id[-6:]}]'
        assert pformat_task_uid('head') == (
            f'{owner.name}[{owner_id[:6]}]'
        )
        assert log_task_uid() == pformat_task_uid()
        assert log_task_uid('head') == pformat_task_uid('head')
        async with trio.open_nursery() as nursery:
            task: trio.lowlevel.Task = await nursery.start(child)
            task_id: str = str(id(task))
            task_uid: str = f'{task.name}[{task_id[-6:]}]'
            report: str = pformat_task(task)
            assert pformat_task_uid(task=task) == task_uid
            assert pformat_task_uid('head', task=task) == (
                f'{task.name}[{task_id[:6]}]'
            )
            assert log_task_uid(task=task) == task_uid
            assert log_task_uid('head', task=task) == (
                pformat_task_uid('head', task=task)
            )
            assert f'uid={task_uid!r}' in report
            assert f'task={task!r}' in report
            assert f'coro={task.coro!r}' in report
            assert f'parent_nursery: {nursery!r}' in report
            assert f'parent_task={owner!r}' in report
            assert 'child_nurseries: ()' in report
            assert f'child_nurseries: ({nursery!r},)' in (
                pformat_task(owner)
            )
            assert pformat_task() == pformat_task(owner)
            nursery.cancel_scope.cancel()

    trio.run(main)


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
