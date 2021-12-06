'''
``async with ():`` inlined context-stream cancellation testing.

Verify the we raise errors when streams are opened prior to sync-opening
a ``tractor.Context`` beforehand.

'''
from itertools import count
import platform
from typing import Optional

import pytest
import trio
import tractor
from tractor._exceptions import StreamOverrun

from conftest import tractor_test

# the general stream semantics are
# - normal termination: far end relays a stop message which
# terminates an ongoing ``MsgStream`` iteration
# - cancel termination: context is cancelled on either side cancelling
#  the "linked" inter-actor task context


_state: bool = False


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

        # teardown the actor
        await portal.cancel_actor()


@tractor.context
async def really_started(
    ctx: tractor.Context,
) -> None:
    await ctx.started()
    try:
        await ctx.started()
    except RuntimeError:
        raise


def test_started_called_more_then_once():

    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                'too_much_starteds',
                enable_modules=[__name__],
            )

            async with portal.open_context(really_started) as (ctx, sent):
                await trio.sleep(1)
                # pass

    with pytest.raises(tractor.RemoteActorError):
        trio.run(main)


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
        ('cancel_caller_during_overrun', 1, never_open_stream),
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
    from tractor._actor import Actor
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
                        for i in range(buf_size):
                            print(f'sending {i}')
                            await stream.send(i)

                        if 'cancel' in overrunner:
                            # without this we block waiting on the child side
                            await ctx.cancel()

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
    if overrunner == 'caller':
        with pytest.raises(tractor.RemoteActorError) as excinfo:
            trio.run(main)

        assert excinfo.value.type == StreamOverrun

    elif 'cancel' in overrunner:
        with pytest.raises(trio.MultiError) as excinfo:
            trio.run(main)

        multierr = excinfo.value

        for exc in multierr.exceptions:
            etype = type(exc)
            if etype == tractor.RemoteActorError:
                assert exc.type == StreamOverrun
            else:
                assert etype == tractor.ContextCancelled

    elif overrunner == 'callee':
        with pytest.raises(tractor.RemoteActorError) as excinfo:
            trio.run(main)

        # TODO: embedded remote errors so that we can verify the source
        # error?
        # the callee delivers an error which is an overrun wrapped
        # in a remote actor error.
        assert excinfo.value.type == tractor.RemoteActorError

    else:
        trio.run(main)


@tractor.context
async def echo_back_sequence(

    ctx:  tractor.Context,
    seq: list[int],
    msg_buffer_size: Optional[int] = None,

) -> None:
    '''
    Send endlessly on the calleee stream.

    '''
    await ctx.started()
    async with ctx.open_stream(
        msg_buffer_size=msg_buffer_size,
    ) as stream:

        seq = list(seq)  # bleh, `msgpack`...
        count = 0
        while count < 3:
            batch = []
            async for msg in stream:
                batch.append(msg)
                if batch == seq:
                    break

            for msg in batch:
                print(f'callee sending {msg}')
                await stream.send(msg)

            count += 1

        return 'yo'


def test_stream_backpressure():
    '''
    Demonstrate small overruns of each task back and forth
    on a stream not raising any errors by default.

    '''
    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                'callee_sends_forever',
                enable_modules=[__name__],
            )
            seq = list(range(3))
            async with portal.open_context(
                echo_back_sequence,
                seq=seq,
                msg_buffer_size=1,
            ) as (ctx, sent):
                assert sent is None

                async with ctx.open_stream(msg_buffer_size=1) as stream:
                    count = 0
                    while count < 3:
                        for msg in seq:
                            print(f'caller sending {msg}')
                            await stream.send(msg)
                            await trio.sleep(0.1)

                        batch = []
                        async for msg in stream:
                            batch.append(msg)
                            if batch == seq:
                                break

                        count += 1

            # here the context should return
            assert await ctx.result() == 'yo'

            # cancel the daemon
            await portal.cancel_actor()

    trio.run(main)
