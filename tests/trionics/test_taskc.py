'''
`tractor.trionics._taskc.start_or_cancel()` unit tests.

`trio.Nursery.start()` collapses an out-of-band (ancestor)
cancellation into a lossy,

  `RuntimeError('child exited without calling
  task_status.started()')`

whenever the started child exits pre-`.started()` WITHOUT
propagating the ambient `trio.Cancelled`; a common outcome
when the child (or any lib code it calls) runs a graceful
teardown which absorbs the cancel and returns early. Our
`start_or_cancel()` wrapper re-surfaces the real in-flight
cancellation in that case so the true root error/cancel
propagates to the `.start()` caller instead.

These tests verify both that repair AND document upstream
`trio`'s current lossy behaviour via the
`use_start_or_cancel=False` parametrizations; if a `trio`
upgrade breaks one of THOSE cases it likely means upstream
shipped better startup-cancellation porcelain and our
wrapper deserves a re-audit!

The core use case was dug out of `modden`'s
`progman.open_wks()` program-spawn machinery as per gh
issue #474; the wrapper landed originally via gh PR #464.

'''
import pytest
import trio
from trio import TaskStatus

from tractor.trionics import start_or_cancel


async def absorbs_cancel_pre_started(
    task_status: TaskStatus[None] = trio.TASK_STATUS_IGNORED,
):
    '''
    Swallow the ambient (ancestor-scope) cancel and return
    early, a naughty-but-realistic graceful-teardown pattern
    and the exact shape which causes `trio.Nursery.start()`
    to raise its lossy startup `RuntimeError` in place of
    the real `trio.Cancelled`.

    '''
    try:
        await trio.sleep(2)
    except trio.Cancelled:
        return
    task_status.started()


async def raise_val_err():
    '''
    Sibling task which blows up (fast) thus OOB-cancelling
    the shared parent-nursery's cancel-scope.

    '''
    await trio.lowlevel.checkpoint()
    raise ValueError('sibling blew up!')


@pytest.mark.parametrize(
    'use_start_or_cancel',
    [
        True,
        False,
    ],
)
def test_sibling_err_not_masked_by_startup_rte(
    use_start_or_cancel: bool,
):
    '''
    The `modden.runtime.progman` use case: a sibling task
    errors while the `.start()`-ed child is still
    pre-`.started()`, OOB-cancelling the shared nursery
    scope; the child absorbs its cancel (graceful teardown)
    and exits early.

    - with `start_or_cancel()` the in-flight cancellation
      is re-surfaced as the real `trio.Cancelled` (then
      absorbed by the cancelled nursery scope) so ONLY the
      root-cause sibling error escapes the nursery.

    - with a bare `.start()`, upstream `trio` (currently)
      also delivers its lossy startup `RuntimeError`
      alongside, obscuring that the child was in fact
      cancelled due to the sibling's error.

    '''
    async def main():
        async with trio.open_nursery() as tn:
            tn.start_soon(raise_val_err)
            if use_start_or_cancel:
                await start_or_cancel(
                    tn,
                    absorbs_cancel_pre_started,
                )
            else:
                await tn.start(absorbs_cancel_pre_started)

    with pytest.raises(ExceptionGroup) as excinfo:
        trio.run(main)

    eg: ExceptionGroup = excinfo.value
    val_eg, rest_eg = eg.split(ValueError)
    assert len(val_eg.exceptions) == 1

    if use_start_or_cancel:
        # the re-surfaced `Cancelled` is absorbed by the
        # (sibling-error cancelled) nursery scope leaving
        # NO startup-noise, just the root cause.
        assert rest_eg is None
    else:
        # the `trio` wart: a lossy startup RTE rides along
        # with (and distracts from) the root cause.
        rte = rest_eg.exceptions[0]
        assert isinstance(rte, RuntimeError)
        assert 'child exited without calling' in rte.args[0]


@pytest.mark.parametrize(
    'use_start_or_cancel',
    [
        True,
        False,
    ],
)
def test_pure_oob_cancel_not_morphed_to_rte(
    use_start_or_cancel: bool,
):
    '''
    A plain (error-free) ancestor `CancelScope.cancel()`
    fired while the (cancel-absorbing) child is still
    pre-`.started()`:

    - `start_or_cancel()` re-surfaces the `Cancelled` so
      the cancelled scope exits CLEAN, no error at all.

    - a bare `.start()` (currently) morphs the plain
      cancel into an (eg-wrapped) startup `RuntimeError`.

    '''
    async def main():
        with trio.CancelScope() as cs:
            async with trio.open_nursery() as tn:

                async def canceller():
                    await trio.lowlevel.checkpoint()
                    cs.cancel()

                tn.start_soon(canceller)
                if use_start_or_cancel:
                    await start_or_cancel(
                        tn,
                        absorbs_cancel_pre_started,
                    )
                else:
                    await tn.start(
                        absorbs_cancel_pre_started,
                    )

        assert cs.cancelled_caught

    if use_start_or_cancel:
        trio.run(main)
    else:
        with pytest.raises(ExceptionGroup) as excinfo:
            trio.run(main)

        rte = excinfo.value.exceptions[0]
        assert isinstance(rte, RuntimeError)
        assert 'child exited without calling' in rte.args[0]


@pytest.mark.parametrize(
    'use_start_or_cancel',
    [
        True,
        False,
    ],
)
def test_genuine_startup_rte_still_raised(
    use_start_or_cancel: bool,
):
    '''
    Absent ANY in-flight cancellation, a child exiting
    cleanly without calling `task_status.started()` is a
    genuine startup-protocol bug; `start_or_cancel()` must
    re-raise the resulting `RuntimeError` exactly like a
    bare `.start()` does.

    '''
    async def exits_wo_started(
        task_status: TaskStatus[None] = (
            trio.TASK_STATUS_IGNORED
        ),
    ):
        await trio.lowlevel.checkpoint()

    async def main():
        async with trio.open_nursery() as tn:
            with pytest.raises(RuntimeError) as excinfo:
                if use_start_or_cancel:
                    await start_or_cancel(
                        tn,
                        exits_wo_started,
                    )
                else:
                    await tn.start(exits_wo_started)

            rte = excinfo.value
            assert (
                'child exited without calling'
                in
                rte.args[0]
            )

    trio.run(main)


@pytest.mark.parametrize(
    'rte_arg',
    [
        # a bare `'started' in args[0]` substring match
        # would (wrongly) demote this one to a `Cancelled`
        # under ambient cancellation.
        'never got started!',
        # non-`str` first-arg edge; must not `TypeError`
        # inside the wrapper's msg-match guard.
        1234,
    ],
)
def test_childs_own_rte_never_demoted_to_cancel(
    rte_arg: str|int,
):
    '''
    A child's OWN `RuntimeError`, one which merely smells
    like `trio`'s startup wording (or carries a non-`str`
    first arg), raised under ambient cancellation must NOT
    be demoted to a `trio.Cancelled` by the exact-msg-match
    guard inside `start_or_cancel()`; the real error must
    always propagate to the caller unchanged.

    '''
    async def cancels_cs_then_raises(
        task_status: TaskStatus[None] = (
            trio.TASK_STATUS_IGNORED
        ),
    ):
        # cancel the ambient (ancestor) scope then raise
        # sync-ly, no checkpoint between, so the child
        # deterministically dies with ITS error while the
        # caller is under effective cancellation.
        cs.cancel()
        raise RuntimeError(rte_arg)

    cs = trio.CancelScope()

    async def main():
        with cs:
            async with trio.open_nursery() as tn:
                await start_or_cancel(
                    tn,
                    cancels_cs_then_raises,
                )

    with pytest.raises(ExceptionGroup) as excinfo:
        trio.run(main)

    rte = excinfo.value.exceptions[0]
    assert isinstance(rte, RuntimeError)
    assert rte.args[0] == rte_arg


def test_started_value_and_args_passthru():
    '''
    Happy path: positional args, the `name=` kwarg and the
    `.started(value)`-delivered value all pass through
    `start_or_cancel()` identically to a bare `.start()`.

    '''
    async def echo_started(
        *args,
        task_status: TaskStatus[tuple] = (
            trio.TASK_STATUS_IGNORED
        ),
    ):
        task_name: str = trio.lowlevel.current_task().name
        task_status.started((
            args,
            task_name,
        ))

    async def main():
        async with trio.open_nursery() as tn:
            (
                args,
                task_name,
            ) = await start_or_cancel(
                tn,
                echo_started,
                'chillin',
                10,
                name='doggy',
            )
            assert args == ('chillin', 10)
            assert task_name == 'doggy'

    trio.run(main)
