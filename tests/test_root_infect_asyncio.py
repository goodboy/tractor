'''
Special attention cases for using "infect `asyncio`" mode from a root
actor; i.e. not using a std `trio.run()` bootstrap.

'''
import asyncio
from functools import partial

import pytest
import trio
import tractor
from tractor import (
    to_asyncio,
)
from tests.test_infected_asyncio import (
    aio_echo_server,
)


@pytest.mark.parametrize(
    'raise_error_mid_stream',
    [
        False,
        Exception,
        KeyboardInterrupt,
    ],
    ids='raise_error={}'.format,
)
def test_infected_root_actor(
    raise_error_mid_stream: bool|Exception,

    # conftest wide
    loglevel: str,
    debug_mode: bool,
):
    '''
    Verify you can run the `tractor` runtime with `Actor.is_infected_aio() == True`
    in the root actor.

    '''
    async def _trio_main():
        with trio.fail_after(2):
            first: str
            chan: to_asyncio.LinkedTaskChannel
            async with (
                tractor.open_root_actor(
                    debug_mode=debug_mode,
                    loglevel=loglevel,
                ),
                to_asyncio.open_channel_from(
                    aio_echo_server,
                ) as (first, chan),
            ):
                assert first == 'start'

                for i in range(1000):
                    await chan.send(i)
                    out = await chan.receive()
                    assert out == i
                    print(f'asyncio echoing {i}')

                    if raise_error_mid_stream and i == 500:
                        raise raise_error_mid_stream

                    if out is None:
                        try:
                            out = await chan.receive()
                        except trio.EndOfChannel:
                            break
                        else:
                            raise RuntimeError(
                                'aio channel never stopped?'
                            )

    if raise_error_mid_stream:
        with pytest.raises(raise_error_mid_stream):
            tractor.to_asyncio.run_as_asyncio_guest(
                trio_main=_trio_main,
            )
    else:
        tractor.to_asyncio.run_as_asyncio_guest(
            trio_main=_trio_main,
        )



async def sync_and_err(
    # just signature placeholders for compat with
    # ``to_asyncio.open_channel_from()``
    to_trio: trio.MemorySendChannel,
    from_trio: asyncio.Queue,
    ev: asyncio.Event,

):
    if to_trio:
        to_trio.send_nowait('start')

    await ev.wait()
    raise RuntimeError('asyncio-side')


@pytest.mark.parametrize(
    'aio_err_trigger',
    [
        'before_start_point',
        'after_trio_task_starts',
        'after_start_point',
    ],
    ids='aio_err_triggered={}'.format
)
def test_trio_prestarted_task_bubbles(
    aio_err_trigger: str,

    # conftest wide
    loglevel: str,
    debug_mode: bool,
):
    async def pre_started_err(
        raise_err: bool = False,
        pre_sleep: float|None = None,
        aio_trigger: asyncio.Event|None = None,
        task_status=trio.TASK_STATUS_IGNORED,
    ):
        '''
        Maybe pre-started error then sleep.

        '''
        if pre_sleep is not None:
            print(f'Sleeping from trio for {pre_sleep!r}s !')
            await trio.sleep(pre_sleep)

        # signal aio-task to raise JUST AFTER this task
        # starts but has not yet `.started()`
        if aio_trigger:
            print('Signalling aio-task to raise from `trio`!!')
            aio_trigger.set()

        if raise_err:
            print('Raising from trio!')
            raise TypeError('trio-side')

        task_status.started()
        await trio.sleep_forever()

    async def _trio_main():
        # with trio.fail_after(2):
        with trio.fail_after(999):
            first: str
            chan: to_asyncio.LinkedTaskChannel
            aio_ev = asyncio.Event()

            async with (
                tractor.open_root_actor(
                    debug_mode=False,
                    loglevel=loglevel,
                ),
            ):
                # TODO, tests for this with 3.13 egs?
                # from tractor.devx import open_crash_handler
                # with open_crash_handler():
                async with (
                    # where we'll start a sub-task that errors BEFORE
                    # calling `.started()` such that the error should
                    # bubble before the guest run terminates!
                    trio.open_nursery() as tn,

                    # THEN start an infect task which should error just
                    # after the trio-side's task does.
                    to_asyncio.open_channel_from(
                        partial(
                            sync_and_err,
                            ev=aio_ev,
                        )
                    ) as (first, chan),
                ):

                    for i in range(5):
                        pre_sleep: float|None = None
                        last_iter: bool = (i == 4)

                        # TODO, missing cases?
                        # -[ ] error as well on
                        #    'after_start_point' case as well for
                        #    another case?
                        raise_err: bool = False

                        if last_iter:
                            raise_err: bool = True

                            # trigger aio task to error on next loop
                            # tick/checkpoint
                            if aio_err_trigger == 'before_start_point':
                                aio_ev.set()

                            pre_sleep: float = 0

                        await tn.start(
                            pre_started_err,
                            raise_err,
                            pre_sleep,
                            (aio_ev if (
                                    aio_err_trigger == 'after_trio_task_starts'
                                    and
                                    last_iter
                                ) else None
                            ),
                        )

                        if (
                            aio_err_trigger == 'after_start_point'
                            and
                            last_iter
                        ):
                            aio_ev.set()

    with pytest.raises(
        expected_exception=ExceptionGroup,
    ) as excinfo:
        tractor.to_asyncio.run_as_asyncio_guest(
            trio_main=_trio_main,
        )

    eg = excinfo.value
    rte_eg, rest_eg = eg.split(RuntimeError)

    # ensure the trio-task's error bubbled despite the aio-side
    # having (maybe) errored first.
    if aio_err_trigger in (
        'after_trio_task_starts',
        'after_start_point',
    ):
        assert len(errs := rest_eg.exceptions) == 1
        typerr = errs[0]
        assert (
            type(typerr) is TypeError
            and
            'trio-side' in typerr.args
        )

    # when aio errors BEFORE (last) trio task is scheduled, we should
    # never see anythinb but the aio-side.
    else:
        assert len(rtes := rte_eg.exceptions) == 1
        assert 'asyncio-side' in rtes[0].args[0]
