'''
Codify the cancellation request semantics in terms
of one remote actor cancelling another.

'''
# from contextlib import asynccontextmanager as acm
import itertools

import pytest
import trio
import tractor
from tractor import (  # typing
    Portal,
    Context,
    ContextCancelled,
)


# def test_self_cancel():
#     '''
#     2 cases:
#     - calls `Actor.cancel()` locally in some task
#     - calls LocalPortal.cancel_actor()` ?

#     '''
#     ...


@tractor.context
async def sleep_forever(
    ctx: Context,
) -> None:
    '''
    Sync the context, open a stream then just sleep.

    '''
    await ctx.started()
    async with ctx.open_stream():
        await trio.sleep_forever()


@tractor.context
async def error_before_started(
    ctx: Context,
) -> None:
    '''
    This simulates exactly an original bug discovered in:
    https://github.com/pikers/piker/issues/244

    Cancel a context **before** any underlying error is raised so
    as to trigger a local reception of a ``ContextCancelled`` which
    SHOULD NOT be re-raised in the local surrounding ``Context``
    *iff* the cancel was requested by **this** (callee)  side of
    the context.

    '''
    async with tractor.wait_for_actor('sleeper') as p2:
        async with (
            p2.open_context(sleep_forever) as (peer_ctx, first),
            peer_ctx.open_stream(),
        ):
            # NOTE: this WAS inside an @acm body but i factored it
            # out and just put it inline here since i don't think
            # the mngr part really matters, though maybe it could?
            try:
                # XXX NOTE XXX: THIS sends an UNSERIALIZABLE TYPE which
                # should raise a `TypeError` and **NOT BE SWALLOWED** by
                # the surrounding try/finally (normally inside the
                # body of some acm)..
                await ctx.started(object())
                # yield
            finally:
                # XXX: previously this would trigger local
                # ``ContextCancelled`` to be received and raised in the
                # local context overriding any local error due to logic
                # inside ``_invoke()`` which checked for an error set on
                # ``Context._error`` and raised it in a cancellation
                # scenario.
                # ------
                # The problem is you can have a remote cancellation that
                # is part of a local error and we shouldn't raise
                # ``ContextCancelled`` **iff** we **were not** the side
                # of the context to initiate it, i.e.
                # ``Context._cancel_called`` should **NOT** have been
                # set. The special logic to handle this case is now
                # inside ``Context._maybe_raise_from_remote_msg()`` XD
                await peer_ctx.cancel()


def test_do_not_swallow_error_before_started_by_remote_contextcancelled():
    '''
    Verify that an error raised in a remote context which itself
    opens YET ANOTHER remote context, which it then cancels, does not
    override the original error that caused the cancellation of the
    secondary context.

    '''
    async def main():
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                'errorer',
                enable_modules=[__name__],
            )
            await n.start_actor(
                'sleeper',
                enable_modules=[__name__],
            )

            async with (
                portal.open_context(
                    error_before_started
                ) as (ctx, sent),
            ):
                await trio.sleep_forever()

    with pytest.raises(tractor.RemoteActorError) as excinfo:
        trio.run(main)

    assert excinfo.value.type == TypeError


@tractor.context
async def sleep_a_bit_then_cancel_peer(
    ctx: Context,
    peer_name: str = 'sleeper',
    cancel_after: float = .5,

) -> None:
    '''
    Connect to peer, sleep as per input delay, cancel the peer.

    '''
    peer: Portal
    async with tractor.wait_for_actor(peer_name) as peer:
        await ctx.started()
        await trio.sleep(cancel_after)
        await peer.cancel_actor()


@tractor.context
async def stream_ints(
    ctx: Context,
):
    await ctx.started()
    async with ctx.open_stream() as stream:
        for i in itertools.count():
            await stream.send(i)


@tractor.context
async def stream_from_peer(
    ctx: Context,
    peer_name: str = 'sleeper',
) -> None:

    peer: Portal
    try:
        async with (
            tractor.wait_for_actor(peer_name) as peer,
            peer.open_context(stream_ints) as (peer_ctx, first),
            peer_ctx.open_stream() as stream,
        ):
            await ctx.started()
            # XXX TODO: big set of questions for this
            # - should we raise `ContextCancelled` or `Cancelled` (rn
            #   it does that) here?!
            # - test the `ContextCancelled` OUTSIDE the
            #   `.open_context()` call?
            try:
                async for msg in stream:
                    print(msg)

            except trio.Cancelled:
                assert not ctx.cancel_called
                assert not ctx.cancelled_caught

                assert not peer_ctx.cancel_called
                assert not peer_ctx.cancelled_caught

                assert 'root' in ctx.cancel_called_remote

                raise  # XXX MUST NEVER MASK IT!!

            with trio.CancelScope(shield=True):
                await tractor.pause()
            # pass
            # pytest.fail(
            raise RuntimeError(
                'peer never triggered local `[Context]Cancelled`?!?'
            )

    # NOTE: cancellation of the (sleeper) peer should always
    # cause a `ContextCancelled` raise in this streaming
    # actor.
    except ContextCancelled as ctxerr:
        assert ctxerr.canceller == 'canceller'
        assert ctxerr._remote_error is ctxerr

        # CASE 1: we were cancelled by our parent, the root actor.
        # TODO: there are other cases depending on how the root
        # actor and it's caller side task are written:
        # - if the root does not req us to cancel then an
        # IPC-transport related error should bubble from the async
        # for loop and thus cause local cancellation both here
        # and in the root (since in that case this task cancels the
        # context with the root, not the other way around)
        assert ctx.cancel_called_remote[0] == 'root'
        raise

    # except BaseException as err:

    #     raise

# cases:
# - some arbitrary remote peer cancels via Portal.cancel_actor().
#   => all other connected peers should get that cancel requesting peer's
#      uid in the ctx-cancelled error msg.

# - peer spawned a sub-actor which (also) spawned a failing task
#   which was unhandled and propagated up to the immediate
#   parent, the peer to the actor that also spawned a remote task
#   task in that same peer-parent.

# - peer cancelled itself - so other peers should
#   get errors reflecting that the peer was itself the .canceller?

# - WE cancelled the peer and thus should not see any raised
#   `ContextCancelled` as it should be reaped silently?
#   => pretty sure `test_context_stream_semantics::test_caller_cancels()`
#      already covers this case?

@pytest.mark.parametrize(
    'error_during_ctxerr_handling',
    [False, True],
)
def test_peer_canceller(
    error_during_ctxerr_handling: bool,
):
    '''
    Verify that a cancellation triggered by an in-actor-tree peer
    results in a cancelled errors with all other actors which have
    opened contexts to that same actor.

    legend:
    name>
        a "play button" that indicates a new runtime instance,
        an individual actor with `name`.

    .subname>
        a subactor who's parent should be on some previous
        line and be less indented.

    .actor0> ()-> .actor1>
        a inter-actor task context opened (by `async with `Portal.open_context()`)
        from actor0 *into* actor1.

    .actor0> ()<=> .actor1>
        a inter-actor task context opened (as above)
        from actor0 *into* actor1 which INCLUDES an additional
        stream open using `async with Context.open_stream()`.


    ------ - ------
    supervision view
    ------ - ------
    root>
     .sleeper> TODO: SOME SYNTAX SHOWING JUST SLEEPING
     .just_caller> ()=> .sleeper>
     .canceller> ()-> .sleeper>
                  TODO:  how define calling `Portal.cancel_actor()`

    In this case a `ContextCancelled` with `.errorer` set to the
    requesting actor, in this case 'canceller', should be relayed
    to all other actors who have also opened a (remote task)
    context with that now cancelled actor.

    ------ - ------
    task view
    ------ - ------
    So there are 5 context open in total with 3 from the root to
    its children and 2 from children to their peers:
    1. root> ()-> .sleeper>
    2. root> ()-> .streamer>
    3. root> ()-> .canceller>

    4. .streamer> ()<=> .sleep>
    5. .canceller> ()-> .sleeper>
        - calls `Portal.cancel_actor()`


    '''

    async def main():
        async with tractor.open_nursery() as an:
            canceller: Portal = await an.start_actor(
                'canceller',
                enable_modules=[__name__],
            )
            sleeper: Portal = await an.start_actor(
                'sleeper',
                enable_modules=[__name__],
            )
            just_caller: Portal = await an.start_actor(
                'just_caller',  # but i just met her?
                enable_modules=[__name__],
            )

            try:
                async with (
                    sleeper.open_context(
                        sleep_forever,
                    ) as (sleeper_ctx, sent),

                    just_caller.open_context(
                        stream_from_peer,
                    ) as (caller_ctx, sent),

                    canceller.open_context(
                        sleep_a_bit_then_cancel_peer,
                    ) as (canceller_ctx, sent),

                ):
                    ctxs: list[Context] = [
                        sleeper_ctx,
                        caller_ctx,
                        canceller_ctx,
                    ]

                    try:
                        print('PRE CONTEXT RESULT')
                        await sleeper_ctx.result()

                        # should never get here
                        pytest.fail(
                            'Context.result() did not raise ctx-cancelled?'
                        )

                    # TODO: not sure why this isn't catching
                    # but maybe we need an `ExceptionGroup` and
                    # the whole except *errs: thinger in 3.11?
                    except ContextCancelled as ctxerr:
                        print(f'CAUGHT REMOTE CONTEXT CANCEL {ctxerr}')

                        # canceller and caller peers should not
                        # have been remotely cancelled.
                        assert canceller_ctx.cancel_called_remote is None
                        assert caller_ctx.cancel_called_remote is None

                        assert ctxerr.canceller[0] == 'canceller'

                        # XXX NOTE XXX: since THIS `ContextCancelled`
                        # HAS NOT YET bubbled up to the
                        # `sleeper.open_context().__aexit__()` this
                        # value is not yet set, however outside this
                        # block it should be.
                        assert not sleeper_ctx.cancelled_caught

                        # TODO: a test which ensures this error is
                        # bubbled and caught (NOT MASKED) by the
                        # runtime!!! 
                        if error_during_ctxerr_handling:
                            raise RuntimeError('Simulated error during teardown')

                        raise

                    # SHOULD NEVER GET HERE!
                    except BaseException:
                        pytest.fail('did not rx ctx-cancelled error?')
                    else:
                        pytest.fail('did not rx ctx-cancelled error?')

            except (
                ContextCancelled,
                RuntimeError,
            )as ctxerr:
                _err = ctxerr

                if error_during_ctxerr_handling:
                    assert isinstance(ctxerr, RuntimeError)

                    # NOTE: this root actor task should have
                    # called `Context.cancel()` on the
                    # `.__aexit__()` to every opened ctx.
                    for ctx in ctxs:
                        assert ctx.cancel_called

                        # each context should have received
                        # a silently absorbed context cancellation
                        # from its peer actor's task.
                        assert ctx.chan.uid == ctx.cancel_called_remote

                        # this root actor task should have
                        # cancelled all opened contexts except
                        # the sleeper which is cancelled by its
                        # peer "canceller"
                        if ctx is not sleeper_ctx:
                            assert ctx._remote_error.canceller[0] == 'root'

                else:
                    assert ctxerr.canceller[0] == 'canceller'

                    # the sleeper's remote error is the error bubbled
                    # out of the context-stack above!
                    re = sleeper_ctx._remote_error
                    assert re is ctxerr

                    for ctx in ctxs:

                        if ctx is sleeper_ctx:
                            assert not ctx.cancel_called
                            assert ctx.cancelled_caught
                        else:
                            assert ctx.cancel_called
                            assert not ctx.cancelled_caught

                        # each context should have received
                        # a silently absorbed context cancellation
                        # from its peer actor's task.
                        assert ctx.chan.uid == ctx.cancel_called_remote

                    # NOTE: when an inter-peer cancellation
                    # occurred, we DO NOT expect this
                    # root-actor-task to have requested a cancel of
                    # the context since cancellation was caused by
                    # the "canceller" peer and thus
                    # `Context.cancel()` SHOULD NOT have been
                    # called inside
                    # `Portal.open_context().__aexit__()`.
                    assert not sleeper_ctx.cancel_called

                # XXX NOTE XXX: and see matching comment above but,
                # this flag is set only AFTER the `.open_context()`
                # has exited and should be set in both outcomes
                # including the case where ctx-cancel handling
                # itself errors.
                assert sleeper_ctx.cancelled_caught
                assert sleeper_ctx.cancel_called_remote[0] == 'sleeper'

                # await tractor.pause()
                raise  # always to ensure teardown

    if error_during_ctxerr_handling:
        with pytest.raises(RuntimeError) as excinfo:
            trio.run(main)
    else:

        with pytest.raises(ContextCancelled) as excinfo:
            trio.run(main)

        assert excinfo.value.type == ContextCancelled
        assert excinfo.value.canceller[0] == 'canceller'
