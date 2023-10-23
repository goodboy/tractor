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

# XXX TODO cases:
# - [ ] peer cancelled itself - so other peers should
#   get errors reflecting that the peer was itself the .canceller?

# - [x] WE cancelled the peer and thus should not see any raised
#   `ContextCancelled` as it should be reaped silently?
#   => pretty sure `test_context_stream_semantics::test_caller_cancels()`
#      already covers this case?

# - [x] INTER-PEER: some arbitrary remote peer cancels via
#   Portal.cancel_actor().
#   => all other connected peers should get that cancel requesting peer's
#      uid in the ctx-cancelled error msg raised in all open ctxs
#      with that peer.

# - [ ] PEER-FAILS-BY-CHILD-ERROR: peer spawned a sub-actor which
#   (also) spawned a failing task which was unhandled and
#   propagated up to the immediate parent - the peer to the actor
#   that also spawned a remote task task in that same peer-parent.


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
    expect_ctxc: bool = False,
) -> None:
    '''
    Sync the context, open a stream then just sleep.

    Allow checking for (context) cancellation locally.

    '''
    try:
        await ctx.started()
        async with ctx.open_stream():
            await trio.sleep_forever()

    except BaseException as berr:

        # TODO: it'd sure be nice to be able to inject our own
        # `ContextCancelled` here instead of of `trio.Cancelled`
        # so that our runtime can expect it and this "user code"
        # would be able to tell the diff between a generic trio
        # cancel and a tractor runtime-IPC cancel.
        if expect_ctxc:
            assert isinstance(berr, trio.Cancelled)

        raise


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
            await trio.sleep(0.01)


@tractor.context
async def stream_from_peer(
    ctx: Context,
    peer_name: str = 'sleeper',
) -> None:

    peer: Portal
    try:
        async with (
            tractor.wait_for_actor(peer_name) as peer,
            # peer.open_context(stream_ints) as (peer_ctx, first),
            # peer_ctx.open_stream() as stream,
        ):
            async with (
                peer.open_context(stream_ints) as (peer_ctx, first),
                # peer_ctx.open_stream() as stream,
            ):
            #     # try:
                async with (
                    peer_ctx.open_stream() as stream,
                ):

                    await ctx.started()
                    # XXX QUESTIONS & TODO: for further details around this
                    # in the longer run..
                    # https://github.com/goodboy/tractor/issues/368
                    # - should we raise `ContextCancelled` or `Cancelled` (rn
                    #   it does latter) and should/could it be implemented
                    #   as a general injection override for `trio` such
                    #   that ANY next checkpoint would raise the "cancel
                    #   error type" of choice?
                    # - should the `ContextCancelled` bubble from
                    #   all `Context` and `MsgStream` apis wherein it
                    #   prolly makes the most sense to make it
                    #   a `trio.Cancelled` subtype?
                    # - what about IPC-transport specific errors, should
                    #   they bubble from the async for and trigger
                    #   other special cases?
                    # try:
                    # NOTE: current ctl flow:
                    # - stream raises `trio.EndOfChannel` and
                    #   exits the loop
                    # - `.open_context()` will raise the ctxcanc
                    #   received from the sleeper.
                    async for msg in stream:
                        assert msg is not None
                        print(msg)
                # finally:
                # await trio.sleep(0.1)
                # from tractor import pause
                # await pause()

            # except BaseException as berr:
            #     with trio.CancelScope(shield=True):
            #         await tractor.pause()
            #     raise

            # except trio.Cancelled:
            #     with trio.CancelScope(shield=True):
            #         await tractor.pause()
            #     raise  # XXX NEVER MASK IT
            # from tractor import pause
            # await pause()

    # NOTE: cancellation of the (sleeper) peer should always
    # cause a `ContextCancelled` raise in this streaming
    # actor.
    except ContextCancelled as ctxerr:
        err = ctxerr
        assert peer_ctx._remote_error is ctxerr
        assert peer_ctx.canceller == ctxerr.canceller

        # caller peer should not be the cancel requester
        assert not ctx.cancel_called
        # XXX can never be true since `._invoke` only
        # sets this AFTER the nursery block this task
        # was started in, exits.
        assert not ctx.cancelled_caught

        # we never requested cancellation
        assert not peer_ctx.cancel_called
        # the `.open_context()` exit definitely
        # caught a cancellation in the internal `Context._scope`
        # since likely the runtime called `_deliver_msg()`
        # after receiving the remote error from the streaming
        # task.
        assert peer_ctx.cancelled_caught

        # TODO / NOTE `.canceller` won't have been set yet
        # here because that machinery is inside
        # `.open_context().__aexit__()` BUT, if we had
        # a way to know immediately (from the last
        # checkpoint) that cancellation was due to
        # a remote, we COULD assert this here..see,
        # https://github.com/goodboy/tractor/issues/368

        # root/parent actor task should NEVER HAVE cancelled us!
        assert not ctx.canceller
        assert 'canceller' in peer_ctx.canceller

        # TODO: IN THEORY we could have other cases depending on
        # who cancels first, the root actor or the canceller peer.
        #
        # 1- when the peer request is first then the `.canceller`
        #   field should obvi be set to the 'canceller' uid,
        #
        # 2-if the root DOES req cancel then we should see the same
        #   `trio.Cancelled` implicitly raised
        # assert ctx.canceller[0] == 'root'
        # assert peer_ctx.canceller[0] == 'sleeper'
        raise

    raise RuntimeError(
        'peer never triggered local `ContextCancelled`?'
    )

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
        a inter-actor task context opened (by `async with
        `Portal.open_context()`) from actor0 *into* actor1.

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
        async with tractor.open_nursery(
            # debug_mode=True
        ) as an:
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

            root = tractor.current_actor()

            try:
                async with (
                    sleeper.open_context(
                        sleep_forever,
                        expect_ctxc=True,
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
                        # await tractor.pause()
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
                        assert canceller_ctx.canceller is None
                        assert caller_ctx.canceller is None

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

                    # XXX SHOULD NEVER EVER GET HERE XXX
                    except BaseException as berr:
                        err = berr
                        pytest.fail('did not rx ctx-cancelled error?')
                    else:
                        pytest.fail('did not rx ctx-cancelled error?')

            except (
                ContextCancelled,
                RuntimeError,
            )as ctxerr:
                _err = ctxerr

                # NOTE: the main state to check on `Context` is:
                # - `.cancelled_caught` (maps to nursery cs)
                # - `.cancel_called` (bool of whether this side
                #    requested)
                # - `.canceller` (uid of cancel-causing actor-task)
                # - `._remote_error` (any `RemoteActorError`
                #    instance from other side of context)
                # - `._cancel_msg` (any msg that caused the
                #    cancel)

                # CASE: error raised during handling of
                # `ContextCancelled` inside `.open_context()`
                # block
                if error_during_ctxerr_handling:
                    assert isinstance(ctxerr, RuntimeError)

                    # NOTE: this root actor task should have
                    # called `Context.cancel()` on the
                    # `.__aexit__()` to every opened ctx.
                    for ctx in ctxs:
                        assert ctx.cancel_called

                        # this root actor task should have
                        # cancelled all opened contexts except the
                        # sleeper which is obvi by the "canceller"
                        # peer.
                        re = ctx._remote_error
                        if (
                            ctx is sleeper_ctx
                            or ctx is caller_ctx
                        ):
                            assert re.canceller == canceller.channel.uid

                        else:
                            assert re.canceller == root.uid

                        # each context should have received
                        # a silently absorbed context cancellation
                        # from its peer actor's task.
                        # assert ctx.chan.uid == ctx.canceller

                # CASE: standard teardown inside in `.open_context()` block
                else:
                    assert ctxerr.canceller == sleeper_ctx.canceller
                    # assert ctxerr.canceller[0] == 'canceller'
                    # assert sleeper_ctx.canceller[0] == 'canceller'

                    # the sleeper's remote error is the error bubbled
                    # out of the context-stack above!
                    re = sleeper_ctx._remote_error
                    assert re is ctxerr

                    for ctx in ctxs:
                        re: BaseException | None = ctx._remote_error
                        assert re

                        # root doesn't cancel sleeper since it's
                        # cancelled by its peer.
                        # match ctx:
                        #     case sleeper_ctx:
                        if ctx is sleeper_ctx:
                            assert not ctx.cancel_called
                            # wait WHY?
                            assert ctx.cancelled_caught

                        elif ctx is caller_ctx:
                            # since its context was remotely
                            # cancelled, we never needed to
                            # call `Context.cancel()` bc our
                            # context was already remotely
                            # cancelled by the time we'd do it.
                            assert ctx.cancel_called

                        else:
                            assert ctx.cancel_called
                            assert not ctx.cancelled_caught

                        # TODO: do we even need this flag?
                        # -> each context should have received
                        # a silently absorbed context cancellation
                        # in its remote nursery scope.
                        # assert ctx.chan.uid == ctx.canceller

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
