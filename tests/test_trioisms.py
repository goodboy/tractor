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
            trio.open_nursery(
                strict_exception_groups=False,
            ) as pn,
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


@pytest.mark.parametrize(
    ('unmask_from_canc', 'canc_from_finally'),
    [
        (True, False),
        (True, True),
        pytest.param(False, True,
                     marks=pytest.mark.xfail(reason="never raises!")
        ),
    ],
    # TODO, ask ronny how to impl this .. XD
    # ids='unmask_from_canc={0}, canc_from_finally={1}',#.format,
)
def test_acm_embedded_nursery_propagates_enter_err(
    canc_from_finally: bool,
    unmask_from_canc: bool,
    debug_mode: bool,
):
    '''
    Demo how a masking `trio.Cancelled` could be handled by unmasking from the
    `.__context__` field when a user (by accident) re-raises from a `finally:`.

    '''
    import tractor

    @acm
    async def wraps_tn_that_always_cancels():
        async with (
            trio.open_nursery() as tn,
            tractor.trionics.maybe_raise_from_masking_exc(
                tn=tn,
                unmask_from=(
                    trio.Cancelled
                    if unmask_from_canc
                    else None
                ),
            )
        ):
            try:
                yield tn
            finally:
                if canc_from_finally:
                    tn.cancel_scope.cancel()
                    await trio.lowlevel.checkpoint()

    async def _main():
        with tractor.devx.maybe_open_crash_handler(
            pdb=debug_mode,
        ) as bxerr:
            if bxerr:
                assert not bxerr.value

            async with (
                wraps_tn_that_always_cancels() as tn,
            ):
                assert not tn.cancel_scope.cancel_called
                assert 0

        assert (
            (err := bxerr.value)
            and
            type(err) is AssertionError
        )

    with pytest.raises(ExceptionGroup) as excinfo:
        trio.run(_main)

    eg: ExceptionGroup = excinfo.value
    assert_eg, rest_eg = eg.split(AssertionError)

    assert len(assert_eg.exceptions) == 1



def test_gatherctxs_with_memchan_breaks_multicancelled(
    debug_mode: bool,
):
    '''
    Demo how a using an `async with sndchan` inside a `.trionics.gather_contexts()` task
    will break a strict-eg-tn's multi-cancelled absorption..

    '''
    from tractor import (
        trionics,
    )

    @acm
    async def open_memchan() -> trio.abc.ReceiveChannel:

        task: trio.Task = trio.lowlevel.current_task()
        print(
            f'Opening {task!r}\n'
        )

        # 1 to force eager sending
        send, recv = trio.open_memory_channel(16)

        try:
            async with send:
                yield recv
        finally:
            print(
                f'Closed {task!r}\n'
            )


    async def main():
        async with (
            # XXX should ensure ONLY the KBI
            # is relayed upward
            trionics.collapse_eg(),
            trio.open_nursery(
                # strict_exception_groups=False,
            ), # as tn,

            trionics.gather_contexts([
                open_memchan(),
                open_memchan(),
            ]) as recv_chans,
        ):
            assert len(recv_chans) == 2

            await trio.sleep(1)
            raise KeyboardInterrupt
            # tn.cancel_scope.cancel()

    with pytest.raises(KeyboardInterrupt):
        trio.run(main)
