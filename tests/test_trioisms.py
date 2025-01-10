'''
Reminders for oddities in `trio` that we need to stay aware of and/or
want to see changed.

'''
from contextlib import (
    asynccontextmanager as acm,
)

import pytest
import trio
from trio import TaskStatus


@pytest.mark.parametrize(
    'use_start_soon', [
        pytest.param(
            True,
            marks=pytest.mark.xfail(reason="see python-trio/trio#2258")
        ),
        False,
    ]
)
def test_stashed_child_nursery(use_start_soon):

    _child_nursery = None

    async def waits_on_signal(
        ev: trio.Event(),
        task_status: TaskStatus[trio.Nursery] = trio.TASK_STATUS_IGNORED,
    ):
        '''
        Do some stuf, then signal other tasks, then yield back to "starter".

        '''
        await ev.wait()
        task_status.started()

    async def mk_child_nursery(
        task_status: TaskStatus = trio.TASK_STATUS_IGNORED,
    ):
        '''
        Allocate a child sub-nursery and stash it as a global.

        '''
        nonlocal _child_nursery

        async with trio.open_nursery() as cn:
            _child_nursery = cn
            task_status.started(cn)

            # block until cancelled by parent.
            await trio.sleep_forever()

    async def sleep_and_err(
        ev: trio.Event,
        task_status: TaskStatus = trio.TASK_STATUS_IGNORED,
    ):
        await trio.sleep(0.5)
        doggy()  # noqa
        ev.set()
        task_status.started()

    async def main():

        async with (
            trio.open_nursery() as pn,
        ):
            cn = await pn.start(mk_child_nursery)
            assert cn

            ev = trio.Event()

            if use_start_soon:
                # this causes inf hang
                cn.start_soon(sleep_and_err, ev)

            else:
                # this does not.
                await cn.start(sleep_and_err, ev)

            with trio.fail_after(1):
                await cn.start(waits_on_signal, ev)

    with pytest.raises(NameError):
        trio.run(main)


# @pytest.mark.parametrize(
#     'open_tn_outside_acm',
#     [True, False]
#     # ids='aio_err_triggered={}'.format
# )
@pytest.mark.parametrize(
    'canc_from_finally',
    [True, False]
    # ids='aio_err_triggered={}'.format
)
def test_acm_embedded_nursery_propagates_enter_err(
    canc_from_finally: bool,
    # open_tn_outside_acm: bool,
):
    # from tractor.trionics import maybe_open_nursery

    # async def canc_then_checkpoint(tn):
    #     tn.cancel_scope.cancel()
    #     await trio.lowlevel.checkpoint()

    @acm
    async def wraps_tn_that_always_cancels(
        # maybe_tn: trio.Nursery|None = None
    ):
        # async with maybe_open_nursery(maybe_tn) as tn:
        async with trio.open_nursery() as tn:
            try:
                yield tn
            finally:
                if canc_from_finally:
                    # await canc_then_checkpoint(tn)
                    tn.cancel_scope.cancel()
                    await trio.lowlevel.checkpoint()

    async def _main():
        # open_nursery = (
        #     trio.open_nursery if open_tn_outside_acm
        #     else nullcontext
        # )

        async with (
            # open_nursery() as tn,
            # wraps_tn_that_always_cancels(maybe_tn=tn) as tn
            wraps_tn_that_always_cancels() as tn
        ):
            assert not tn.cancel_scope.cancel_called
            assert 0

    with pytest.raises(ExceptionGroup) as excinfo:
        trio.run(_main)

    eg = excinfo.value
    assert_eg, rest_eg = eg.split(AssertionError)

    assert len(assert_eg.exceptions) == 1
