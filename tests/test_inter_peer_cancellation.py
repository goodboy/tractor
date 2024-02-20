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


def test_do_not_swallow_error_before_started_by_remote_contextcancelled(
    debug_mode: bool,
):
    '''
    Verify that an error raised in a remote context which itself
    opens YET ANOTHER remote context, which it then cancels, does not
    override the original error that caused the cancellation of the
    secondary context.

    '''
    async def main():
        async with tractor.open_nursery(
            debug_mode=debug_mode,
        ) as n:
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
            peer.open_context(stream_ints) as (peer_ctx, first),
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
            # NOTE: current ctl flow:
            # - stream raises `trio.EndOfChannel` and
            #   exits the loop
            # - `.open_context()` will raise the ctxcanc
            #   received from the sleeper.
            async for msg in stream:
                assert msg is not None
                print(msg)

    # NOTE: cancellation of the (sleeper) peer should always
    # cause a `ContextCancelled` raise in this streaming
    # actor.
    except ContextCancelled as ctxc:
        ctxerr = ctxc

        assert peer_ctx._remote_error is ctxerr
        assert peer_ctx._remote_error.msgdata == ctxerr.msgdata
        assert peer_ctx.canceller == ctxerr.canceller

        # caller peer should not be the cancel requester
        assert not ctx.cancel_called

        # XXX can never be true since `._invoke` only
        # sets this AFTER the nursery block this task
        # was started in, exits.
        assert not ctx.cancelled_caught

        # we never requested cancellation
        assert not peer_ctx.cancel_called
        # the `.open_context()` exit definitely caught
        # a cancellation in the internal `Context._scope` since
        # likely the runtime called `_deliver_msg()` after
        # receiving the remote error from the streaming task.
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

        raise
        # TODO: IN THEORY we could have other cases depending on
        # who cancels first, the root actor or the canceller peer?.
        #
        # 1- when the peer request is first then the `.canceller`
        #   field should obvi be set to the 'canceller' uid,
        #
        # 2-if the root DOES req cancel then we should see the same
        #   `trio.Cancelled` implicitly raised
        # assert ctx.canceller[0] == 'root'
        # assert peer_ctx.canceller[0] == 'sleeper'

    raise RuntimeError('Never triggered local `ContextCancelled` ?!?')


@pytest.mark.parametrize(
    'error_during_ctxerr_handling',
    [False, True],
)
def test_peer_canceller(
    error_during_ctxerr_handling: bool,
    debug_mode: bool,
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
            # NOTE: to halt the peer tasks on ctxc, uncomment this.
            debug_mode=debug_mode,
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
                        res = await sleeper_ctx.result()
                        assert res

                        # should never get here
                        pytest.fail(
                            'Context.result() did not raise ctx-cancelled?'
                        )

                    # should always raise since this root task does
                    # not request the sleeper cancellation ;)
                    except ContextCancelled as ctxerr:
                        print(
                            'CAUGHT REMOTE CONTEXT CANCEL FOM\n'
                            f'{ctxerr}'
                        )

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

                        if error_during_ctxerr_handling:
                            raise RuntimeError('Simulated error during teardown')

                        raise

                    # XXX SHOULD NEVER EVER GET HERE XXX
                    except BaseException as berr:
                        raise

                        # XXX if needed to debug failure
                        # _err = berr
                        # await tractor.pause()
                        # await trio.sleep_forever()

                        pytest.fail(
                            'did not rx ctxc ?!?\n\n'

                            f'{berr}\n'
                        )

                    else:
                        pytest.fail(
                            'did not rx ctxc ?!?\n\n'

                            f'{ctxs}\n'
                        )

            except (
                ContextCancelled,
                RuntimeError,
            )as loc_err:
                _loc_err = loc_err

                # NOTE: the main state to check on `Context` is:
                # - `.cancelled_caught` (maps to nursery cs)
                # - `.cancel_called` (bool of whether this side
                #    requested)
                # - `.canceller` (uid of cancel-causing actor-task)
                # - `._remote_error` (any `RemoteActorError`
                #    instance from other side of context)
                # TODO: are we really planning to use this tho?
                # - `._cancel_msg` (any msg that caused the
                #    cancel)

                # CASE: error raised during handling of
                # `ContextCancelled` inside `.open_context()`
                # block
                if error_during_ctxerr_handling:
                    assert isinstance(loc_err, RuntimeError)

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
                            assert (
                                re.canceller
                                ==
                                ctx.canceller
                                ==
                                canceller.channel.uid
                            )

                        else:
                            assert (
                                re.canceller
                                ==
                                ctx.canceller
                                ==
                                root.uid
                            )

                # CASE: standard teardown inside in `.open_context()` block
                else:
                    assert isinstance(loc_err, ContextCancelled)
                    assert loc_err.canceller == sleeper_ctx.canceller
                    assert (
                        loc_err.canceller[0]
                        ==
                        sleeper_ctx.canceller[0]
                        ==
                        'canceller'
                    )

                    # the sleeper's remote error is the error bubbled
                    # out of the context-stack above!
                    re = sleeper_ctx._remote_error
                    assert re is loc_err

                    for ctx in ctxs:
                        re: BaseException | None = ctx._remote_error
                        assert re

                        # root doesn't cancel sleeper since it's
                        # cancelled by its peer.
                        if ctx is sleeper_ctx:
                            assert not ctx.cancel_called
                            # since sleeper_ctx.result() IS called
                            # above we should have (silently)
                            # absorbed the corresponding
                            # `ContextCancelled` for it and thus
                            # the logic inside `.cancelled_caught`
                            # should trigger!
                            assert ctx.cancelled_caught

                        elif ctx is caller_ctx:
                            # since its context was remotely
                            # cancelled, we never needed to
                            # call `Context.cancel()` bc it was
                            # done by the peer and also we never 
                            assert ctx.cancel_called

                            # TODO: figure out the details of
                            # this..
                            # if you look the `._local_error` here
                            # is a multi of ctxc + 2 Cancelleds?
                            # assert not ctx.cancelled_caught

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

                raise  # always to ensure teardown

    if error_during_ctxerr_handling:
        with pytest.raises(RuntimeError) as excinfo:
            trio.run(main)
    else:

        with pytest.raises(ContextCancelled) as excinfo:
            trio.run(main)

        assert excinfo.value.type == ContextCancelled
        assert excinfo.value.canceller[0] == 'canceller'


def test_client_tree_spawns_and_cancels_service_subactor():
    ...
# TODO: test for the modden `mod wks open piker` bug!
# -> start actor-tree (server) that offers sub-actor spawns via
#   context API
# -> start another full actor-tree (client) which requests to the first to
#   spawn over its `@context` ep / api.
# -> client actor cancels the context and should exit gracefully
#   and the server's spawned child should cancel and terminate!
