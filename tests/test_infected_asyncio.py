'''
The hipster way to force SC onto the stdlib's "async": 'infection mode'.

'''
import asyncio
import builtins
from contextlib import ExitStack
# from functools import partial
import itertools
import importlib
import os
from pathlib import Path
import signal
from typing import (
    Callable,
    Iterable,
    Union,
)

import pytest
import trio
import tractor
from tractor import (
    current_actor,
    Actor,
    to_asyncio,
    RemoteActorError,
    ContextCancelled,
    _state,
)
from tractor.trionics import BroadcastReceiver
from tractor._testing import expect_ctxc


@pytest.fixture(
    scope='module',
)
def delay(debug_mode: bool) -> int:
    if debug_mode:
        return 999
    else:
        return 1


async def sleep_and_err(
    sleep_for: float = 0.1,

    # just signature placeholders for compat with
    # ``to_asyncio.open_channel_from()``
    to_trio: trio.MemorySendChannel|None = None,
    from_trio: asyncio.Queue|None = None,

):
    if to_trio:
        to_trio.send_nowait('start')

    await asyncio.sleep(sleep_for)
    assert 0


async def aio_sleep_forever():
    await asyncio.sleep(float('inf'))


async def trio_cancels_single_aio_task():

    # spawn an ``asyncio`` task to run a func and return result
    with trio.move_on_after(.2):
        await tractor.to_asyncio.run_task(aio_sleep_forever)


def test_trio_cancels_aio_on_actor_side(
    reg_addr: tuple[str, int],
    delay: int,
    debug_mode: bool,
):
    '''
    Spawn an infected actor that is cancelled by the ``trio`` side
    task using std cancel scope apis.

    '''
    async def main():
        with trio.fail_after(1 + delay):
            async with tractor.open_nursery(
                registry_addrs=[reg_addr],
                debug_mode=debug_mode,
            ) as an:
                await an.run_in_actor(
                    trio_cancels_single_aio_task,
                    infect_asyncio=True,
                )

    trio.run(main)


async def asyncio_actor(
    target: str,
    expect_err: Exception|None = None

) -> None:

    # ensure internal runtime state is consistent
    actor: Actor = tractor.current_actor()
    assert (
        actor.is_infected_aio()
        and
        actor._infected_aio
        and
        _state._runtime_vars['_is_infected_aio']
    )

    target: Callable = globals()[target]

    if '.' in expect_err:
        modpath, _, name = expect_err.rpartition('.')
        mod = importlib.import_module(modpath)
        error_type = getattr(mod, name)

    else:  # toplevel builtin error type
        error_type = builtins.__dict__.get(expect_err)

    try:
        # spawn an ``asyncio`` task to run a func and return result
        await tractor.to_asyncio.run_task(target)

    except BaseException as err:
        if expect_err:
            assert isinstance(err, error_type), (
                f'{type(err)} is not {error_type}?'
            )

        raise


def test_aio_simple_error(
    reg_addr: tuple[str, int],
    debug_mode: bool,
):
    '''
    Verify a simple remote asyncio error propagates back through trio
    to the parent actor.


    '''
    async def main():
        async with tractor.open_nursery(
            registry_addrs=[reg_addr],
            debug_mode=debug_mode,
        ) as an:
            await an.run_in_actor(
                asyncio_actor,
                target='sleep_and_err',
                expect_err='AssertionError',
                infect_asyncio=True,
            )

    with pytest.raises(
        expected_exception=(RemoteActorError, ExceptionGroup),
    ) as excinfo:
        trio.run(main)

    err = excinfo.value

    # might get multiple `trio.Cancelled`s as well inside an inception
    if isinstance(err, ExceptionGroup):
        err = next(itertools.dropwhile(
            lambda exc: not isinstance(exc, tractor.RemoteActorError),
            err.exceptions
        ))
        assert err

    assert isinstance(err, RemoteActorError)
    assert err.boxed_type is AssertionError


def test_tractor_cancels_aio(
    reg_addr: tuple[str, int],
    debug_mode: bool,
):
    '''
    Verify we can cancel a spawned asyncio task gracefully.

    '''
    async def main():
        async with tractor.open_nursery(
            debug_mode=debug_mode,
        ) as an:
            portal = await an.run_in_actor(
                asyncio_actor,
                target='aio_sleep_forever',
                expect_err='trio.Cancelled',
                infect_asyncio=True,
            )
            # cancel the entire remote runtime
            await portal.cancel_actor()

    trio.run(main)


def test_trio_cancels_aio(
    reg_addr: tuple[str, int],
):
    '''
    Much like the above test with ``tractor.Portal.cancel_actor()``
    except we just use a standard ``trio`` cancellation api.

    '''
    async def main():

        with trio.move_on_after(1):
            # cancel the nursery shortly after boot

            async with tractor.open_nursery() as tn:
                await tn.run_in_actor(
                    asyncio_actor,
                    target='aio_sleep_forever',
                    expect_err='trio.Cancelled',
                    infect_asyncio=True,
                )

    trio.run(main)


@tractor.context
async def trio_ctx(
    ctx: tractor.Context,
):

    await ctx.started('start')

    # this will block until the ``asyncio`` task sends a "first"
    # message.
    delay: int = 999 if tractor.debug_mode() else 1
    with trio.fail_after(1 + delay):
        try:
            async with (
                trio.open_nursery(
                    # TODO, for new `trio` / py3.13
                    # strict_exception_groups=False,
                ) as tn,
                tractor.to_asyncio.open_channel_from(
                    sleep_and_err,
                ) as (first, chan),
            ):

                assert first == 'start'

                # spawn another asyncio task for the cuck of it.
                tn.start_soon(
                    tractor.to_asyncio.run_task,
                    aio_sleep_forever,
                )
                await trio.sleep_forever()

        # TODO, factor this into a `trionics.collapse()`?
        except* BaseException as beg:
            # await tractor.pause(shield=True)
            if len(excs := beg.exceptions) == 1:
                raise excs[0]
            else:
                raise


@pytest.mark.parametrize(
    'parent_cancels',
    ['context', 'actor', False],
    ids='parent_actor_cancels_child={}'.format
)
def test_context_spawns_aio_task_that_errors(
    reg_addr: tuple[str, int],
    delay: int,
    parent_cancels: bool,
    debug_mode: bool,
):
    '''
    Verify that spawning a task via an intertask channel ctx mngr that
    errors correctly propagates the error back from the `asyncio`-side
    task.

    '''
    async def main():
        with trio.fail_after(1 + delay):
            async with tractor.open_nursery() as an:
                p = await an.start_actor(
                    'aio_daemon',
                    enable_modules=[__name__],
                    infect_asyncio=True,
                    debug_mode=debug_mode,
                    loglevel='cancel',
                )
                async with (
                    expect_ctxc(
                        yay=parent_cancels == 'actor',
                    ),
                    p.open_context(
                        trio_ctx,
                    ) as (ctx, first),
                ):

                    assert first == 'start'

                    if parent_cancels == 'actor':
                        await p.cancel_actor()

                    elif parent_cancels == 'context':
                        await ctx.cancel()

                    else:
                        await trio.sleep_forever()

                async with expect_ctxc(
                    yay=parent_cancels == 'actor',
                ):
                    await ctx.result()

                if parent_cancels == 'context':
                    # to tear down sub-acor
                    await p.cancel_actor()

        return ctx.outcome

    if parent_cancels:
        # bc the parent made the cancel request,
        # the error is not raised locally but instead
        # the context is exited silently
        res = trio.run(main)
        assert isinstance(res, ContextCancelled)
        assert 'root' in res.canceller[0]

    else:
        expect = RemoteActorError
        with pytest.raises(expect) as excinfo:
            trio.run(main)

        err = excinfo.value
        assert isinstance(err, expect)
        assert err.boxed_type is AssertionError


async def aio_cancel():
    ''''
    Cancel urself boi.

    '''
    await asyncio.sleep(0.5)

    # cancel and enter sleep
    task = asyncio.current_task()
    task.cancel()
    await aio_sleep_forever()


def test_aio_cancelled_from_aio_causes_trio_cancelled(
    reg_addr: tuple,
    delay: int,
):
    '''
    When the `asyncio.Task` cancels itself the `trio` side should
    also cancel and teardown and relay the cancellation cross-process
    to the parent caller.

    '''
    async def main():

        an: tractor.ActorNursery
        async with tractor.open_nursery() as an:
            p: tractor.Portal = await an.run_in_actor(
                asyncio_actor,
                target='aio_cancel',
                expect_err='tractor.to_asyncio.AsyncioCancelled',
                infect_asyncio=True,
            )
            # NOTE: normally the `an.__aexit__()` waits on the
            # portal's result but we do it explicitly here
            # to avoid indent levels.
            with trio.fail_after(1 + delay):
                await p.wait_for_result()

    with pytest.raises(
        expected_exception=(RemoteActorError, ExceptionGroup),
    ) as excinfo:
        trio.run(main)

    # might get multiple `trio.Cancelled`s as well inside an inception
    err: RemoteActorError|ExceptionGroup = excinfo.value
    if isinstance(err, ExceptionGroup):
        excs = err.exceptions
        assert len(excs) == 1
        final_exc = excs[0]
        assert isinstance(final_exc, tractor.RemoteActorError)

    # relayed boxed error should be our `trio`-task's
    # cancel-signal-proxy-equivalent of `asyncio.CancelledError`.
    assert err.boxed_type == to_asyncio.AsyncioCancelled


# TODO: verify open_channel_from will fail on this..
async def no_to_trio_in_args():
    pass


async def push_from_aio_task(
    sequence: Iterable,
    to_trio: trio.abc.SendChannel,
    expect_cancel: False,
    fail_early: bool,
    exit_early: bool,

) -> None:

    try:
        # print('trying breakpoint')
        # breakpoint()

        # sync caller ctx manager
        to_trio.send_nowait(True)

        for i in sequence:
            print(f'asyncio sending {i}')
            to_trio.send_nowait(i)
            await asyncio.sleep(0.001)

            if (
                i == 50
            ):
                if fail_early:
                    print('Raising exc from aio side!')
                    raise Exception

                if exit_early:
                    # TODO? really you could enforce the same
                    # SC-proto we use for actors here with asyncio
                    # such that a Return[None] msg would be
                    # implicitly delivered to the trio side?
                    #
                    # XXX => this might be the end-all soln for
                    # converting any-inter-task system (regardless
                    # of maybe-remote runtime or language) to be
                    # SC-compat no?
                    print(f'asyncio breaking early @ {i!r}')
                    break

        print('asyncio streaming complete!')

    except asyncio.CancelledError:
        if not expect_cancel:
            pytest.fail("aio task was cancelled unexpectedly")
        raise
    else:
        if expect_cancel:
            pytest.fail("aio task wasn't cancelled as expected!?")


async def stream_from_aio(
    trio_exit_early: bool = False,
    trio_raise_err: bool = False,
    aio_raise_err: bool = False,
    aio_exit_early: bool = False,
    fan_out: bool = False,

) -> None:
    seq = range(100)
    expect = list(seq)

    try:
        pulled = []

        async with to_asyncio.open_channel_from(
            push_from_aio_task,
            sequence=seq,
            expect_cancel=trio_raise_err or trio_exit_early,
            fail_early=aio_raise_err,
            exit_early=aio_exit_early,

            # such that we can test exit early cases
            # for each side explicitly.
            suppress_graceful_exits=(not(
                aio_exit_early
                or
                trio_exit_early
            ))

        ) as (first, chan):

            assert first is True

            async def consume(
                chan: Union[
                    to_asyncio.LinkedTaskChannel,
                    BroadcastReceiver,
                ],
            ):
                async for value in chan:
                    print(f'trio received: {value!r}')

                    # XXX, debugging EoC not being handled correctly
                    # in `transate_aio_errors()`..
                    # if value is None:
                    #     await tractor.pause(shield=True)

                    pulled.append(value)

                    if value == 50:
                        if trio_raise_err:
                            raise Exception
                        elif trio_exit_early:
                            print('`consume()` breaking early!\n')
                            break

                print('returning from `consume()`..\n')

            # run 2 tasks each pulling from
            # the inter-task-channel with the 2nd
            # using a fan-out `BroadcastReceiver`.
            if fan_out:
                async with (

                    # NOTE: this has to come first to avoid
                    # the channel being closed before the nursery
                    # tasks are joined..
                    chan.subscribe() as br,

                    trio.open_nursery() as tn,
                ):
                    # start 2nd task that get's broadcast the same
                    # value set.
                    tn.start_soon(consume, br)
                    await consume(chan)

            else:
                await consume(chan)
    except BaseException as err:
        import logging
        log = logging.getLogger()
        log.exception('aio-subactor errored!\n')
        raise err

    finally:

        if not (
            trio_raise_err
            or
            trio_exit_early
            or
            aio_raise_err
            or
            aio_exit_early
        ):
            if fan_out:
                # we get double the pulled values in the
                # ``.subscribe()`` fan out case.
                doubled = list(itertools.chain(*zip(expect, expect)))
                expect = doubled[:len(pulled)]
                assert list(sorted(pulled)) == expect

            else:
                # await tractor.pause()
                assert pulled == expect
        else:
            assert not fan_out
            assert pulled == expect[:51]

        print('trio guest-mode task completed!')
        assert chan._aio_task.done()


@pytest.mark.parametrize(
    'fan_out', [False, True],
    ids='fan_out_w_chan_subscribe={}'.format
)
def test_basic_interloop_channel_stream(
    reg_addr: tuple[str, int],
    fan_out: bool,
):
    async def main():
        # TODO, figure out min timeout here!
        with trio.fail_after(6):
            async with tractor.open_nursery() as an:
                portal = await an.run_in_actor(
                    stream_from_aio,
                    infect_asyncio=True,
                    fan_out=fan_out,
                )
                # should raise RAE diectly
                await portal.result()

    trio.run(main)


# TODO: parametrize the above test and avoid the duplication here?
def test_trio_error_cancels_intertask_chan(reg_addr):
    async def main():
        async with tractor.open_nursery() as an:
            portal = await an.run_in_actor(
                stream_from_aio,
                trio_raise_err=True,
                infect_asyncio=True,
            )
            # should trigger remote actor error
            await portal.result()

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    # ensure boxed error type
    excinfo.value.boxed_type is Exception


def test_trio_closes_early_causes_aio_checkpoint_raise(
    reg_addr: tuple[str, int],
    delay: int,
    debug_mode: bool,
):
    '''
    Check that if the `trio`-task "exits early and silently" (in this
    case during `async for`-ing the inter-task-channel via
    a `break`-from-loop), we raise `TrioTaskExited` on the
    `asyncio`-side which also then bubbles up through the
    `open_channel_from()` block indicating that the `asyncio.Task`
    hit a ran another checkpoint despite the `trio.Task` exit.

    '''
    async def main():
        with trio.fail_after(1 + delay):
            async with tractor.open_nursery(
                debug_mode=debug_mode,
                # enable_stack_on_sig=True,
            ) as an:
                portal = await an.run_in_actor(
                    stream_from_aio,
                    trio_exit_early=True,
                    infect_asyncio=True,
                )
                # should raise RAE diectly
                print('waiting on final infected subactor result..')
                res: None = await portal.wait_for_result()
                assert res is None
                print(f'infected subactor returned result: {res!r}\n')

    # should be a quiet exit on a simple channel exit
    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    # ensure remote error is an explicit `AsyncioCancelled` sub-type
    # which indicates to the aio task that the trio side exited
    # silently WITHOUT raising a `trio.Cancelled` (which would
    # normally be raised instead as a `AsyncioCancelled`).
    excinfo.value.boxed_type is to_asyncio.TrioTaskExited


def test_aio_exits_early_relays_AsyncioTaskExited(
    # TODO, parametrize the 3 possible trio side conditions:
    # - trio blocking on receive, aio exits early
    # - trio cancelled AND aio exits early on its next tick
    # - trio errors AND aio exits early on its next tick
    reg_addr: tuple[str, int],
    debug_mode: bool,
    delay: int,
):
    '''
    Check that if the `asyncio`-task "exits early and silently" (in this
    case during `push_from_aio_task()` pushing to the `InterLoopTaskChannel`
    it `break`s from the loop), we raise `AsyncioTaskExited` on the
    `trio`-side which then DOES NOT BUBBLE up through the
    `open_channel_from()` block UNLESS,

    - the trio.Task also errored/cancelled, in which case we wrap
      both errors in an eg
    - the trio.Task was blocking on rxing a value from the
      `InterLoopTaskChannel`.

    '''
    async def main():
        with trio.fail_after(1 + delay):
            async with tractor.open_nursery(
                debug_mode=debug_mode,
                # enable_stack_on_sig=True,
            ) as an:
                portal = await an.run_in_actor(
                    stream_from_aio,
                    infect_asyncio=True,
                    trio_exit_early=False,
                    aio_exit_early=True,
                )
                # should raise RAE diectly
                print('waiting on final infected subactor result..')
                res: None = await portal.wait_for_result()
                assert res is None
                print(f'infected subactor returned result: {res!r}\n')

    # should be a quiet exit on a simple channel exit
    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    exc = excinfo.value

    # TODO, wow bug!
    # -[ ] bp handler not replaced!?!?
    # breakpoint()

    # import pdbp; pdbp.set_trace()

    # ensure remote error is an explicit `AsyncioCancelled` sub-type
    # which indicates to the aio task that the trio side exited
    # silently WITHOUT raising a `trio.Cancelled` (which would
    # normally be raised instead as a `AsyncioCancelled`).
    assert exc.boxed_type is to_asyncio.AsyncioTaskExited


def test_aio_errors_and_channel_propagates_and_closes(
    reg_addr: tuple[str, int],
    debug_mode: bool,
):
    async def main():
        async with tractor.open_nursery(
            debug_mode=debug_mode,
        ) as an:
            portal = await an.run_in_actor(
                stream_from_aio,
                aio_raise_err=True,
                infect_asyncio=True,
            )
            # should trigger RAE directly, not an eg.
            await portal.result()

    with pytest.raises(
        # NOTE: bc we directly wait on `Portal.result()` instead
        # of capturing it inside the `ActorNursery` machinery.
        expected_exception=RemoteActorError,
    ) as excinfo:
        trio.run(main)

    excinfo.value.boxed_type is Exception


async def aio_echo_server(
    to_trio: trio.MemorySendChannel,
    from_trio: asyncio.Queue,
) -> None:

    to_trio.send_nowait('start')

    while True:
        try:
            msg = await from_trio.get()
        except to_asyncio.TrioTaskExited:
            print(
                'breaking aio echo loop due to `trio` exit!'
            )
            break

        # echo the msg back
        to_trio.send_nowait(msg)

        # if we get the terminate sentinel
        # break the echo loop
        if msg is None:
            print('breaking aio echo loop')
            break

    print('exiting asyncio task')


@tractor.context
async def trio_to_aio_echo_server(
    ctx: tractor.Context|None,
):
    async with to_asyncio.open_channel_from(
        aio_echo_server,
    ) as (first, chan):
        assert first == 'start'

        await ctx.started(first)

        async with ctx.open_stream() as stream:
            async for msg in stream:
                print(f'asyncio echoing {msg}')
                await chan.send(msg)

                out = await chan.receive()
                # echo back to parent actor-task
                await stream.send(out)

                if out is None:
                    try:
                        out = await chan.receive()
                    except trio.EndOfChannel:
                        break
                    else:
                        raise RuntimeError('aio channel never stopped?')


@pytest.mark.parametrize(
    'raise_error_mid_stream',
    [False, Exception, KeyboardInterrupt],
    ids='raise_error={}'.format,
)
def test_echoserver_detailed_mechanics(
    reg_addr: tuple[str, int],
    debug_mode: bool,
    raise_error_mid_stream,
):
    async def main():
        async with tractor.open_nursery(
            debug_mode=debug_mode,
        ) as an:
            p = await an.start_actor(
                'aio_server',
                enable_modules=[__name__],
                infect_asyncio=True,
            )
            async with p.open_context(
                trio_to_aio_echo_server,
            ) as (ctx, first):

                assert first == 'start'

                async with ctx.open_stream() as stream:
                    for i in range(100):
                        await stream.send(i)
                        out = await stream.receive()
                        assert i == out

                        if raise_error_mid_stream and i == 50:
                            raise raise_error_mid_stream

                    # send terminate msg
                    await stream.send(None)
                    out = await stream.receive()
                    assert out is None

                    if out is None:
                        # ensure the stream is stopped
                        # with trio.fail_after(0.1):
                        try:
                            await stream.receive()
                        except trio.EndOfChannel:
                            pass
                        else:
                            pytest.fail(
                                'stream not stopped after sentinel ?!'
                            )

            # TODO: the case where this blocks and
            # is cancelled by kbi or out of task cancellation
            await p.cancel_actor()

    if raise_error_mid_stream:
        with pytest.raises(raise_error_mid_stream):
            trio.run(main)

    else:
        trio.run(main)


@tractor.context
async def manage_file(
    ctx: tractor.Context,
    tmp_path_str: str,
    send_sigint_to: str,
    trio_side_is_shielded: bool = True,
    bg_aio_task: bool = False,
):
    '''
    Start an `asyncio` task that just sleeps after registering a context
    with `Actor.lifetime_stack`. Trigger a SIGINT to kill the actor tree
    and ensure the stack is closed in the infected mode child.

    To verify the teardown state just write a tmpfile to the `testdir`
    and delete it on actor close.

    '''

    tmp_path: Path = Path(tmp_path_str)
    tmp_file: Path = tmp_path / f'{" ".join(ctx._actor.uid)}.file'

    # create a the tmp file and tell the parent where it's at
    assert not tmp_file.is_file()
    tmp_file.touch()

    stack: ExitStack = current_actor().lifetime_stack
    stack.callback(tmp_file.unlink)

    await ctx.started((
        str(tmp_file),
        os.getpid(),
    ))

    # expect to be cancelled from here!
    try:

        # NOTE: turns out you don't even need to sched an aio task
        # since the original issue, even though seemingly was due to
        # the guest-run being abandoned + a `.debug.pause()` inside
        # `._runtime._async_main()` (which was originally trying to
        # debug the `.lifetime_stack` not closing), IS NOT actually
        # the core issue?
        #
        # further notes:
        #
        # - `trio` only issues the " RuntimeWarning: Trio guest run
        #   got abandoned without properly finishing... weird stuff
        #   might happen" IFF you DO run a asyncio task here, BUT
        # - the original issue of the `.lifetime_stack` not closing
        #   will still happen even if you don't run an `asyncio` task
        #   here even though the "abandon" messgage won't be shown..
        #
        # => ????? honestly i'm lost but it seems to be some issue
        #   with `asyncio` and SIGINT..
        #
        # honestly, this REALLY reminds me why i haven't used
        # `asyncio` by choice in years.. XD
        #
        async with trio.open_nursery() as tn:
            if bg_aio_task:
                tn.start_soon(
                    tractor.to_asyncio.run_task,
                    aio_sleep_forever,
                )

            # XXX don't-need/doesn't-make-a-diff right
            # since we're already doing it from parent?
            # if send_sigint_to == 'child':
            #     os.kill(
            #         os.getpid(),
            #         signal.SIGINT,
            #     )

            # XXX spend a half sec doing shielded checkpointing to
            # ensure that despite the `trio`-side task ignoring the
            # SIGINT, the `asyncio` side won't abandon the guest-run!
            if trio_side_is_shielded:
                with trio.CancelScope(shield=True):
                    for i in range(5):
                        await trio.sleep(0.1)

            await trio.sleep_forever()

    # signalled manually at the OS level (aka KBI) by the parent actor.
    except KeyboardInterrupt:
        print('child raised KBI..')
        assert tmp_file.exists()
        raise

    raise RuntimeError('shoulda received a KBI?')


@pytest.mark.parametrize(
    'trio_side_is_shielded',
    [
        False,
        True,
    ],
    ids=[
        'trio_side_no_shielding',
        'trio_side_does_shielded_work',
    ],
)
@pytest.mark.parametrize(
    'send_sigint_to',
    [
        'child',
        'parent',
    ],
    ids='send_SIGINT_to={}'.format,
)
@pytest.mark.parametrize(
    'bg_aio_task',
    [
        False,

        # NOTE: (and see notes in `manage_file()` above as well) if
        # we FOR SURE SPAWN AN AIO TASK in the child it seems the
        # "silent-abandon" case (as is described in detail in
        # `to_asyncio.run_as_asyncio_guest()`) does not happen and
        # `asyncio`'s loop will at least abandon the `trio` side
        # loudly? .. prolly the state-spot to start looking for
        # a soln that results in NO ABANDONMENT.. XD
        True,
    ],
    ids=[
        'bg_aio_task',
        'just_trio_slee',
    ],
)
@pytest.mark.parametrize(
    'wait_for_ctx',
    [
        False,
        True,
    ],
    ids=[
        'raise_KBI_in_rent',
        'wait_for_ctx',
    ],
)
def test_sigint_closes_lifetime_stack(
    tmp_path: Path,
    wait_for_ctx: bool,
    bg_aio_task: bool,
    trio_side_is_shielded: bool,
    debug_mode: bool,
    send_sigint_to: str,
):
    '''
    Ensure that an infected child can use the `Actor.lifetime_stack`
    to make a file on boot and it's automatically cleaned up by the
    actor-lifetime-linked exit stack closure.

    '''
    async def main():

        delay = 999 if tractor.debug_mode() else 1
        try:
            an: tractor.ActorNursery
            async with tractor.open_nursery(
                debug_mode=debug_mode,
            ) as an:
                p: tractor.Portal = await an.start_actor(
                    'file_mngr',
                    enable_modules=[__name__],
                    infect_asyncio=True,
                )
                async with p.open_context(
                    manage_file,
                    tmp_path_str=str(tmp_path),
                    send_sigint_to=send_sigint_to,
                    bg_aio_task=bg_aio_task,
                    trio_side_is_shielded=trio_side_is_shielded,
                ) as (ctx, first):

                    path_str, cpid = first
                    tmp_file: Path = Path(path_str)
                    assert tmp_file.exists()

                    # XXX originally to simulate what (hopefully)
                    # the below now triggers.. had to manually
                    # trigger a SIGINT from a ctl-c in the root.
                    # await trio.sleep_forever()

                    # XXX NOTE XXX signal infected-`asyncio` child to
                    # OS-cancel with SIGINT; this should trigger the
                    # bad `asyncio` cancel behaviour that can cause
                    # a guest-run abandon as was seen causing
                    # shm-buffer leaks in `piker`'s live quote stream
                    # susbys!
                    #
                    await trio.sleep(.2)
                    pid: int = (
                        cpid if send_sigint_to == 'child'
                        else os.getpid()
                    )
                    os.kill(
                        pid,
                        signal.SIGINT,
                    )

                    # XXX CASE 1: without the bug fixed, in
                    # the non-KBI-raised-in-parent case, this
                    # timeout should trigger!
                    if wait_for_ctx:
                        print('waiting for ctx outcome in parent..')
                        try:
                            with trio.fail_after(1 + delay):
                                await ctx.wait_for_result()
                        except tractor.ContextCancelled as ctxc:
                            assert ctxc.canceller == ctx.chan.uid
                            raise

                    # XXX CASE 2: this seems to be the source of the
                    # original issue which exhibited BEFORE we put
                    # a `Actor.cancel_soon()` inside
                    # `run_as_asyncio_guest()`..
                    else:
                        raise KeyboardInterrupt

                pytest.fail('should have raised some kinda error?!?')

        except (
            KeyboardInterrupt,
            ContextCancelled,
        ):
            # XXX CASE 2: without the bug fixed, in the
            # KBI-raised-in-parent case, the actor teardown should
            # never get run (silently abaondoned by `asyncio`..) and
            # thus the file should leak!
            assert not tmp_file.exists()
            assert ctx.maybe_error

    trio.run(main)



# ?TODO asyncio.Task fn-deco?
# -[ ] do sig checkingat import time like @context?
# -[ ] maybe name it @aio_task ??
# -[ ] chan: to_asyncio.InterloopChannel ??
async def raise_before_started(
    # from_trio: asyncio.Queue,
    # to_trio: trio.abc.SendChannel,
    chan: to_asyncio.LinkedTaskChannel,

) -> None:
    '''
    `asyncio.Task` entry point which RTEs before calling
    `to_trio.send_nowait()`.

    '''
    await asyncio.sleep(0.2)
    raise RuntimeError('Some shite went wrong before `.send_nowait()`!!')

    # to_trio.send_nowait('Uhh we shouldve RTE-d ^^ ??')
    chan.started_nowait('Uhh we shouldve RTE-d ^^ ??')
    await asyncio.sleep(float('inf'))


@tractor.context
async def caching_ep(
    ctx: tractor.Context,
):

    log = tractor.log.get_logger('caching_ep')
    log.info('syncing via `ctx.started()`')
    await ctx.started()

    # XXX, allocate the `open_channel_from()` inside
    # a `.trionics.maybe_open_context()`.
    chan: to_asyncio.LinkedTaskChannel
    async with (
        tractor.trionics.maybe_open_context(
            acm_func=tractor.to_asyncio.open_channel_from,
            kwargs={
                'target': raise_before_started,
                # ^XXX, kwarg to `open_channel_from()`
            },

            # lock around current actor task access
            key=tractor.current_actor().uid,

        ) as (cache_hit, (clients, chan)),
    ):
        if cache_hit:
            log.error(
                'Re-using cached `.open_from_channel()` call!\n'
            )

        else:
            log.info(
                'Allocating SHOULD-FAIL `.open_from_channel()`\n'
            )

        await trio.sleep_forever()


def test_aio_side_raises_before_started(
    reg_addr: tuple[str, int],
    debug_mode: bool,
    loglevel: str,
):
    '''
    Simulates connection-err from `piker.brokers.ib.api`..

    Ensure any error raised by child-`asyncio.Task` BEFORE
    `chan.started()`

    '''
    # delay = 999 if debug_mode else 1
    async def main():
        with trio.fail_after(3):
            an: tractor.ActorNursery
            async with tractor.open_nursery(
                debug_mode=debug_mode,
                loglevel=loglevel,
            ) as an:
                p: tractor.Portal = await an.start_actor(
                    'lchan_cacher_that_raises_fast',
                    enable_modules=[__name__],
                    infect_asyncio=True,
                )
                async with p.open_context(
                    caching_ep,
                ) as (ctx, first):
                    assert not first

    with pytest.raises(
        expected_exception=(RemoteActorError),
    ) as excinfo:
        trio.run(main)

    # ensure `asyncio.Task` exception is bubbled
    # allll the way erp!!
    rae = excinfo.value
    assert rae.boxed_type is RuntimeError

# TODO: debug_mode tests once we get support for `asyncio`!
#
# -[ ] need tests to wrap both scripts:
#   - [ ] infected_asyncio_echo_server.py
#   - [ ] debugging/asyncio_bp.py
#  -[ ] consider moving ^ (some of) these ^ to `test_debugger`?
#
# -[ ] missing impl outstanding includes:
#  - [x] for sync pauses we need to ensure we open yet another
#    `greenback` portal in the asyncio task
#    => completed using `.bestow_portal(task)` inside
#     `.to_asyncio._run_asyncio_task()` right?
#   -[ ] translation func to get from `asyncio` task calling to 
#     `.debug.wait_for_parent_stdin_hijack()` which does root
#     call to do TTY locking.
#
def test_sync_breakpoint():
    '''
    Verify we can do sync-func/code breakpointing using the
    `breakpoint()` builtin inside infected mode actors.

    '''
    pytest.xfail('This support is not implemented yet!')


def test_debug_mode_crash_handling():
    '''
    Verify mult-actor crash handling works with a combo of infected-`asyncio`-mode
    and normal `trio` actors despite nested process trees.

    '''
    pytest.xfail('This support is not implemented yet!')
