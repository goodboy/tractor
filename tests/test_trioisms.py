'''
Reminders for oddities in `trio` that we need to stay aware of and/or
want to see changed.

'''
import pytest
import trio
from trio_typing import TaskStatus


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
