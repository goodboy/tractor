"""
Bidirectional streaming and context API.

"""
import pytest
import trio
import tractor

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

    async def main():

        with trio.fail_after(1.5):
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
    '''callee calls `Context.cancel()` while streaming and caller
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
async def simple_rpc(

    ctx: tractor.Context,
    data: int,

) -> None:
    """Test a small ping-pong server.

    """
    # signal to parent that we're up
    await ctx.started(data + 1)

    print('opening stream in callee')
    async with ctx.open_stream() as stream:

        count = 0
        while True:
            try:
                await stream.receive() == 'ping'
            except trio.EndOfChannel:
                assert count == 10
                break
            else:
                print('pong')
                await stream.send('pong')
                count += 1


@tractor.context
async def simple_rpc_with_forloop(

    ctx: tractor.Context,
    data: int,

) -> None:
    """Same as previous test but using ``async for`` syntax/api.

    """

    # signal to parent that we're up
    await ctx.started(data + 1)

    print('opening stream in callee')
    async with ctx.open_stream() as stream:

        count = 0
        async for msg in stream:

            assert msg == 'ping'
            print('pong')
            await stream.send('pong')
            count += 1

        else:
            assert count == 10


@pytest.mark.parametrize(
    'use_async_for',
    [True, False],
)
@pytest.mark.parametrize(
    'server_func',
    [simple_rpc, simple_rpc_with_forloop],
)
def test_simple_rpc(server_func, use_async_for):
    """The simplest request response pattern.

    """
    async def main():
        async with tractor.open_nursery() as n:

            portal = await n.start_actor(
                'rpc_server',
                enable_modules=[__name__],
            )

            async with portal.open_context(
                server_func,  # taken from pytest parameterization
                data=10,
            ) as (ctx, sent):

                assert sent == 11

                async with ctx.open_stream() as stream:

                    if use_async_for:

                        count = 0
                        # receive msgs using async for style
                        print('ping')
                        await stream.send('ping')

                        async for msg in stream:
                            assert msg == 'pong'
                            print('ping')
                            await stream.send('ping')
                            count += 1

                            if count >= 9:
                                break

                    else:
                        # classic send/receive style
                        for _ in range(10):

                            print('ping')
                            await stream.send('ping')
                            assert await stream.receive() == 'pong'

                # stream should terminate here

            # final context result(s) should be consumed here in __aexit__()

            await portal.cancel_actor()

    trio.run(main)
