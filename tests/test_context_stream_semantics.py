'''
``async with ():`` inlined context-stream cancellation testing.

Verify the we raise errors when streams are opened prior to
sync-opening a ``tractor.Context`` beforehand.

'''
# from contextlib import asynccontextmanager as acm
from itertools import count
import platform
from typing import Optional

import pytest
import trio
import tractor
from tractor._exceptions import (
    StreamOverrun,
    ContextCancelled,
)

from conftest import tractor_test

# ``Context`` semantics are as follows,
#  ------------------------------------

# - standard setup/teardown:
#   ``Portal.open_context()`` starts a new
#   remote task context in another actor. The target actor's task must
#   call ``Context.started()`` to unblock this entry on the caller side.
#   the callee task executes until complete and returns a final value
#   which is delivered to the caller side and retreived via
#   ``Context.result()``.

# - cancel termination:
#   context can be cancelled on either side where either end's task can
#   call ``Context.cancel()`` which raises a local ``trio.Cancelled``
#   and sends a task cancel request to the remote task which in turn
#   raises a ``trio.Cancelled`` in that scope, catches it, and re-raises
#   as ``ContextCancelled``. This is then caught by
#   ``Portal.open_context()``'s exit and we get a graceful termination
#   of the linked tasks.

# - error termination:
#   error is caught after all context-cancel-scope tasks are cancelled
#   via regular ``trio`` cancel scope semantics, error is sent to other
#   side and unpacked as a `RemoteActorError`.


# ``Context.open_stream() as stream: MsgStream:`` msg semantics are:
#  -----------------------------------------------------------------

# - either side can ``.send()`` which emits a 'yield' msgs and delivers
#   a value to the a ``MsgStream.receive()`` call.

# - stream closure: one end relays a 'stop' message which terminates an
#   ongoing ``MsgStream`` iteration.

# - cancel/error termination: as per the context semantics above but
#   with implicit stream closure on the cancelling end.


_state: bool = False


@tractor.context
async def too_many_starteds(
    ctx: tractor.Context,
) -> None:
    '''
    Call ``Context.started()`` more then once (an error).

    '''
    await ctx.started()
    try:
        await ctx.started()
    except RuntimeError:
        raise


@tractor.context
async def not_started_but_stream_opened(
    ctx: tractor.Context,
) -> None:
    '''
    Enter ``Context.open_stream()`` without calling ``.started()``.

    '''
    try:
        async with ctx.open_stream():
            assert 0
    except RuntimeError:
        raise


@pytest.mark.parametrize(
    'target',
    [
        too_many_starteds,
        not_started_but_stream_opened,
    ],
    ids='misuse_type={}'.format,
)
def test_started_misuse(target):

    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                target.__name__,
                enable_modules=[__name__],
            )

            async with portal.open_context(target) as (ctx, sent):
                await trio.sleep(1)

    with pytest.raises(tractor.RemoteActorError):
        trio.run(main)


@tractor.context
async def simple_setup_teardown(

    ctx: tractor.Context,
    data: int,
    block_forever: bool = False,

) -> None:

    # startup phase
    global _state
    _state = True

    # signal to parent that we're up
    await ctx.started(data + 1)

    try:
        if block_forever:
            # block until cancelled
            await trio.sleep_forever()
        else:
            return 'yo'
    finally:
        _state = False


async def assert_state(value: bool):
    global _state
    assert _state == value


@pytest.mark.parametrize(
    'error_parent',
    [False, ValueError, KeyboardInterrupt],
)
@pytest.mark.parametrize(
    'callee_blocks_forever',
    [False, True],
    ids=lambda item: f'callee_blocks_forever={item}'
)
@pytest.mark.parametrize(
    'pointlessly_open_stream',
    [False, True],
    ids=lambda item: f'open_stream={item}'
)
def test_simple_context(
    error_parent,
    callee_blocks_forever,
    pointlessly_open_stream,
):

    timeout = 1.5 if not platform.system() == 'Windows' else 4

    async def main():

        with trio.fail_after(timeout):
            async with tractor.open_nursery() as nursery:

                portal = await nursery.start_actor(
                    'simple_context',
                    enable_modules=[__name__],
                )

                try:
                    async with portal.open_context(
                        simple_setup_teardown,
                        data=10,
                        block_forever=callee_blocks_forever,
                    ) as (ctx, sent):

                        assert sent == 11

                        if callee_blocks_forever:
                            await portal.run(assert_state, value=True)
                        else:
                            assert await ctx.result() == 'yo'

                        if not error_parent:
                            await ctx.cancel()

                        if pointlessly_open_stream:
                            async with ctx.open_stream():
                                if error_parent:
                                    raise error_parent

                                if callee_blocks_forever:
                                    await ctx.cancel()
                                else:
                                    # in this case the stream will send a
                                    # 'stop' msg to the far end which needs
                                    # to be ignored
                                    pass
                        else:
                            if error_parent:
                                raise error_parent

                finally:

                    # after cancellation
                    if not error_parent:
                        await portal.run(assert_state, value=False)

                    # shut down daemon
                    await portal.cancel_actor()

    if error_parent:
        try:
            trio.run(main)
        except error_parent:
            pass
        except trio.MultiError as me:
            # XXX: on windows it seems we may have to expect the group error
            from tractor._exceptions import is_multi_cancelled
            assert is_multi_cancelled(me)
    else:
        trio.run(main)


@pytest.mark.parametrize(
    'callee_returns_early',
    [True, False],
    ids=lambda item: f'callee_returns_early={item}'
)
@pytest.mark.parametrize(
    'cancel_method',
    ['ctx', 'portal'],
    ids=lambda item: f'cancel_method={item}'
)
@pytest.mark.parametrize(
    'chk_ctx_result_before_exit',
    [True, False],
    ids=lambda item: f'chk_ctx_result_before_exit={item}'
)
def test_caller_cancels(
    cancel_method: str,
    chk_ctx_result_before_exit: bool,
    callee_returns_early: bool,
):
    '''
    Verify that when the opening side of a context (aka the caller)
    cancels that context, the ctx does not raise a cancelled when
    either calling `.result()` or on context exit.

    '''
    async def check_canceller(
        ctx: tractor.Context,
    ) -> None:
        # should not raise yet return the remote
        # context cancelled error.
        res = await ctx.result()

        if callee_returns_early:
            assert res == 'yo'

        else:
            err = res
            assert isinstance(err, ContextCancelled)
            assert (
                tuple(err.canceller)
                ==
                tractor.current_actor().uid
            )

    async def main():
        async with tractor.open_nursery() as nursery:
            portal = await nursery.start_actor(
                'simple_context',
                enable_modules=[__name__],
            )
            timeout = 0.5 if not callee_returns_early else 2
            with trio.fail_after(timeout):
                async with portal.open_context(
                    simple_setup_teardown,
                    data=10,
                    block_forever=not callee_returns_early,
                ) as (ctx, sent):

                    if callee_returns_early:
                        # ensure we block long enough before sending
                        # a cancel such that the callee has already
                        # returned it's result.
                        await trio.sleep(0.5)

                    if cancel_method == 'ctx':
                        await ctx.cancel()
                    else:
                        await portal.cancel_actor()

                    if chk_ctx_result_before_exit:
                        await check_canceller(ctx)

            if not chk_ctx_result_before_exit:
                await check_canceller(ctx)

            if cancel_method != 'portal':
                await portal.cancel_actor()

    trio.run(main)


# basic stream terminations:
# - callee context closes without using stream
# - caller context closes without using stream
# - caller context calls `Context.cancel()` while streaming
#   is ongoing resulting in callee being cancelled
# - callee calls `Context.cancel()` while streaming and caller
#   sees stream terminated in `RemoteActorError`

# TODO: future possible features
# - restart request: far end raises `ContextRestart`


@tractor.context
async def close_ctx_immediately(

    ctx: tractor.Context,

) -> None:

    await ctx.started()
    global _state

    async with ctx.open_stream():
        pass


@tractor_test
async def test_callee_closes_ctx_after_stream_open():
    'callee context closes without using stream'

    async with tractor.open_nursery() as n:

        portal = await n.start_actor(
            'fast_stream_closer',
            enable_modules=[__name__],
        )

        with trio.fail_after(2):
            async with portal.open_context(
                close_ctx_immediately,

                # flag to avoid waiting the final result
                # cancel_on_exit=True,

            ) as (ctx, sent):

                assert sent is None

                with trio.fail_after(0.5):
                    async with ctx.open_stream() as stream:

                        # should fall through since ``StopAsyncIteration``
                        # should be raised through translation of
                        # a ``trio.EndOfChannel`` by
                        # ``trio.abc.ReceiveChannel.__anext__()``
                        async for _ in stream:
                            assert 0
                        else:

                            # verify stream is now closed
                            try:
                                await stream.receive()
                            except trio.EndOfChannel:
                                pass

                # TODO: should be just raise the closed resource err
                # directly here to enforce not allowing a re-open
                # of a stream to the context (at least until a time of
                # if/when we decide that's a good idea?)
                try:
                    with trio.fail_after(0.5):
                        async with ctx.open_stream() as stream:
                            pass
                except trio.ClosedResourceError:
                    pass

        await portal.cancel_actor()


@tractor.context
async def expect_cancelled(

    ctx: tractor.Context,

) -> None:
    global _state
    _state = True

    await ctx.started()

    try:
        async with ctx.open_stream() as stream:
            async for msg in stream:
                await stream.send(msg)  # echo server

    except trio.Cancelled:
        # expected case
        _state = False
        raise

    else:
        assert 0, "Wasn't cancelled!?"


@pytest.mark.parametrize(
    'use_ctx_cancel_method',
    [False, True],
)
@tractor_test
async def test_caller_closes_ctx_after_callee_opens_stream(
    use_ctx_cancel_method: bool,
):
    'caller context closes without using stream'

    async with tractor.open_nursery() as n:

        portal = await n.start_actor(
            'ctx_cancelled',
            enable_modules=[__name__],
        )

        async with portal.open_context(
            expect_cancelled,
        ) as (ctx, sent):
            await portal.run(assert_state, value=True)

            assert sent is None

            # call cancel explicitly
            if use_ctx_cancel_method:

                await ctx.cancel()

                try:
                    async with ctx.open_stream() as stream:
                        async for msg in stream:
                            pass

                except tractor.ContextCancelled:
                    raise  # XXX: must be propagated to __aexit__

                else:
                    assert 0, "Should have context cancelled?"

                # channel should still be up
                assert portal.channel.connected()

                # ctx is closed here
                await portal.run(assert_state, value=False)

            else:
                try:
                    with trio.fail_after(0.2):
                        await ctx.result()
                        assert 0, "Callee should have blocked!?"
                except trio.TooSlowError:
                    await ctx.cancel()
        try:
            async with ctx.open_stream() as stream:
                async for msg in stream:
                    pass
        except tractor.ContextCancelled:
            pass
        else:
            assert 0, "Should have received closed resource error?"

        # ctx is closed here
        await portal.run(assert_state, value=False)

        # channel should not have been destroyed yet, only the
        # inter-actor-task context
        assert portal.channel.connected()

        # teardown the actor
        await portal.cancel_actor()


@tractor_test
async def test_multitask_caller_cancels_from_nonroot_task():

    async with tractor.open_nursery() as n:

        portal = await n.start_actor(
            'ctx_cancelled',
            enable_modules=[__name__],
        )

        async with portal.open_context(
            expect_cancelled,
        ) as (ctx, sent):

            await portal.run(assert_state, value=True)
            assert sent is None

            async with ctx.open_stream() as stream:

                async def send_msg_then_cancel():
                    await stream.send('yo')
                    await portal.run(assert_state, value=True)
                    await ctx.cancel()
                    await portal.run(assert_state, value=False)

                async with trio.open_nursery() as n:
                    n.start_soon(send_msg_then_cancel)

                    try:
                        async for msg in stream:
                            assert msg == 'yo'

                    except tractor.ContextCancelled:
                        raise  # XXX: must be propagated to __aexit__

                # channel should still be up
                assert portal.channel.connected()

                # ctx is closed here
                await portal.run(assert_state, value=False)

        # channel should not have been destroyed yet, only the
        # inter-actor-task context
        assert portal.channel.connected()

        # teardown the actor
        await portal.cancel_actor()


@tractor.context
async def cancel_self(

    ctx: tractor.Context,

) -> None:
    global _state
    _state = True

    await ctx.cancel()

    # should inline raise immediately
    try:
        async with ctx.open_stream():
            pass
    except tractor.ContextCancelled:
        # suppress for now so we can do checkpoint tests below
        pass
    else:
        raise RuntimeError('Context didnt cancel itself?!')

    # check a real ``trio.Cancelled`` is raised on a checkpoint
    try:
        with trio.fail_after(0.1):
            await trio.sleep_forever()
    except trio.Cancelled:
        raise

    except trio.TooSlowError:
        # should never get here
        assert 0


@tractor_test
async def test_callee_cancels_before_started():
    '''
    Callee calls `Context.cancel()` while streaming and caller
    sees stream terminated in `ContextCancelled`.

    '''
    async with tractor.open_nursery() as n:

        portal = await n.start_actor(
            'cancels_self',
            enable_modules=[__name__],
        )
        try:

            async with portal.open_context(
                cancel_self,
            ) as (ctx, sent):
                async with ctx.open_stream():
                    await trio.sleep_forever()

        # raises a special cancel signal
        except tractor.ContextCancelled as ce:
            ce.type == trio.Cancelled

            # the traceback should be informative
            assert 'cancelled itself' in ce.msgdata['tb_str']

        # teardown the actor
        await portal.cancel_actor()


@tractor.context
async def never_open_stream(

    ctx:  tractor.Context,

) -> None:
    '''
    Context which never opens a stream and blocks.

    '''
    await ctx.started()
    await trio.sleep_forever()


@tractor.context
async def keep_sending_from_callee(

    ctx:  tractor.Context,
    msg_buffer_size: Optional[int] = None,

) -> None:
    '''
    Send endlessly on the calleee stream.

    '''
    await ctx.started()
    async with ctx.open_stream(
        msg_buffer_size=msg_buffer_size,
    ) as stream:
        for msg in count():
            print(f'callee sending {msg}')
            await stream.send(msg)
            await trio.sleep(0.01)


@pytest.mark.parametrize(
    'overrun_by',
    [
        ('caller', 1, never_open_stream),
        ('callee', 0, keep_sending_from_callee),
    ],
    ids='overrun_condition={}'.format,
)
def test_one_end_stream_not_opened(overrun_by):
    '''
    This should exemplify the bug from:
    https://github.com/goodboy/tractor/issues/265

    '''
    overrunner, buf_size_increase, entrypoint = overrun_by
    from tractor._runtime import Actor
    buf_size = buf_size_increase + Actor.msg_buffer_size

    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                entrypoint.__name__,
                enable_modules=[__name__],
            )

            async with portal.open_context(
                entrypoint,
            ) as (ctx, sent):
                assert sent is None

                if 'caller' in overrunner:

                    async with ctx.open_stream() as stream:

                        # itersend +1 msg more then the buffer size
                        # to cause the most basic overrun.
                        for i in range(buf_size):
                            print(f'sending {i}')
                            await stream.send(i)

                        else:
                            # expect overrun error to be relayed back
                            # and this sleep interrupted
                            await trio.sleep_forever()

                else:
                    # callee overruns caller case so we do nothing here
                    await trio.sleep_forever()

            await portal.cancel_actor()

    # 2 overrun cases and the no overrun case (which pushes right up to
    # the msg limit)
    if (
        overrunner == 'caller'
    ):
        with pytest.raises(tractor.RemoteActorError) as excinfo:
            trio.run(main)

        assert excinfo.value.type == StreamOverrun

    elif overrunner == 'callee':
        with pytest.raises(tractor.RemoteActorError) as excinfo:
            trio.run(main)

        # TODO: embedded remote errors so that we can verify the source
        # error? the callee delivers an error which is an overrun
        # wrapped in a remote actor error.
        assert excinfo.value.type == tractor.RemoteActorError

    else:
        trio.run(main)


@tractor.context
async def echo_back_sequence(

    ctx:  tractor.Context,
    seq: list[int],
    wait_for_cancel: bool,
    allow_overruns_side: str,
    be_slow: bool = False,
    msg_buffer_size: int = 1,

) -> None:
    '''
    Send endlessly on the calleee stream using a small buffer size
    setting on the contex to simulate backlogging that would normally
    cause overruns.

    '''
    # NOTE: ensure that if the caller is expecting to cancel this task
    # that we stay echoing much longer then they are so we don't
    # return early instead of receive the cancel msg.
    total_batches: int = 1000 if wait_for_cancel else 6

    await ctx.started()
    # await tractor.breakpoint()
    async with ctx.open_stream(
        msg_buffer_size=msg_buffer_size,

        # literally the point of this test XD
        allow_overruns=(allow_overruns_side in {'child', 'both'}),
    ) as stream:

        # ensure mem chan settings are correct
        assert (
            ctx._send_chan._state.max_buffer_size
            ==
            msg_buffer_size
        )

        seq = list(seq)  # bleh, msgpack sometimes ain't decoded right
        for _ in range(total_batches):
            batch = []
            async for msg in stream:
                batch.append(msg)
                if batch == seq:
                    break

                if be_slow:
                    await trio.sleep(0.05)

                print('callee waiting on next')

            for msg in batch:
                print(f'callee sending {msg}')
                await stream.send(msg)

    print(
        'EXITING CALLEEE:\n'
        f'{ctx.cancel_called_remote}'
    )
    return 'yo'


@pytest.mark.parametrize(
    # aka the side that will / should raise
    # and overrun under normal conditions.
    'allow_overruns_side',
    ['parent', 'child', 'none', 'both'],
    ids=lambda item: f'allow_overruns_side={item}'
)
@pytest.mark.parametrize(
    # aka the side that will / should raise
    # and overrun under normal conditions.
    'slow_side',
    ['parent', 'child'],
    ids=lambda item: f'slow_side={item}'
)
@pytest.mark.parametrize(
    'cancel_ctx',
    [True, False],
    ids=lambda item: f'cancel_ctx={item}'
)
def test_maybe_allow_overruns_stream(
    cancel_ctx: bool,
    slow_side: str,
    allow_overruns_side: str,
    loglevel: str,
):
    '''
    Demonstrate small overruns of each task back and forth
    on a stream not raising any errors by default by setting
    the ``allow_overruns=True``.

    The original idea here was to show that if you set the feeder mem
    chan to a size smaller then the # of msgs sent you could could not
    get a `StreamOverrun` crash plus maybe get all the msgs that were
    sent. The problem with the "real backpressure" case is that due to
    the current arch it can result in the msg loop being blocked and thus
    blocking cancellation - which is like super bad. So instead this test
    had to be adjusted to more or less just "not send overrun errors" so
    as to handle the case where the sender just moreso cares about not getting
    errored out when it send to fast..

    '''
    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                'callee_sends_forever',
                enable_modules=[__name__],
                loglevel=loglevel,

                # debug_mode=True,
            )
            seq = list(range(10))
            async with portal.open_context(
                echo_back_sequence,
                seq=seq,
                wait_for_cancel=cancel_ctx,
                be_slow=(slow_side == 'child'),
                allow_overruns_side=allow_overruns_side,
            ) as (ctx, sent):

                assert sent is None

                async with ctx.open_stream(
                    msg_buffer_size=1 if slow_side == 'parent' else None,
                    allow_overruns=(allow_overruns_side in {'parent', 'both'}),
                ) as stream:

                    total_batches: int = 2
                    for _ in range(total_batches):
                        for msg in seq:
                            # print(f'root tx {msg}')
                            await stream.send(msg)
                            if slow_side == 'parent':
                                # NOTE: we make the parent slightly
                                # slower, when it is slow, to make sure
                                # that in the overruns everywhere case
                                await trio.sleep(0.16)

                        batch = []
                        async for msg in stream:
                            print(f'root rx {msg}')
                            batch.append(msg)
                            if batch == seq:
                                break

                if cancel_ctx:
                    # cancel the remote task
                    print('sending root side cancel')
                    await ctx.cancel()

            res = await ctx.result()

            if cancel_ctx:
                assert isinstance(res, ContextCancelled)
                assert tuple(res.canceller) == tractor.current_actor().uid

            else:
                print(f'RX ROOT SIDE RESULT {res}')
                assert res == 'yo'

            # cancel the daemon
            await portal.cancel_actor()

    if (
        allow_overruns_side == 'both'
        or slow_side == allow_overruns_side
    ):
        trio.run(main)

    elif (
        slow_side != allow_overruns_side
    ):

        with pytest.raises(tractor.RemoteActorError) as excinfo:
            trio.run(main)

        err = excinfo.value

        if (
            allow_overruns_side == 'none'
        ):
            # depends on timing is is racy which side will
            # overrun first :sadkitty:

            # NOTE: i tried to isolate to a deterministic case here
            # based on timeing, but i was kinda wasted, and i don't
            # think it's sane to catch them..
            assert err.type in (
                tractor.RemoteActorError,
                StreamOverrun,
            )

        elif (
            slow_side == 'child'
        ):
            assert err.type == StreamOverrun

        elif slow_side == 'parent':
            assert err.type == tractor.RemoteActorError
            assert 'StreamOverrun' in err.msgdata['tb_str']

    else:
        # if this hits the logic blocks from above are not
        # exhaustive..
        pytest.fail('PARAMETRIZED CASE GEN PROBLEM YO')
