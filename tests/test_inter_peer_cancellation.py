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
    Actor,
    current_actor,
    open_nursery,
    Portal,
    Context,
    ContextCancelled,
    RemoteActorError,
)
from tractor._testing import (
    # tractor_test,
    expect_ctxc,
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
async def open_stream_then_sleep_forever(
    ctx: Context,
    expect_ctxc: bool = False,

) -> None:
    '''
    Sync the context, open a stream then just sleep.

    Allow checking for (context) cancellation locally.

    '''
    try:
        await ctx.started()

        # NOTE: the below means this child will send a `Stop`
        # to it's parent-side task despite that side never
        # opening a stream itself.
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
            p2.open_context(open_stream_then_sleep_forever) as (peer_ctx, first),
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

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    rae = excinfo.value
    assert rae.boxed_type is TypeError


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

        # such that we're cancelled by our rent ctx-task
        await trio.sleep(3)
        print('CANCELLER RETURNING!')


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
    debug_mode: bool,
    peer_name: str = 'sleeper',
) -> None:

    # sanity
    assert tractor._state.debug_mode() == debug_mode

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
            #
            # NOTE: current ctl flow:
            # - stream raises `trio.EndOfChannel` and
            #   exits the loop
            # - `.open_context()` will raise the ctxc received
            #   from the sleeper.
            async for msg in stream:
                assert msg is not None
                print(msg)

    # NOTE: cancellation of the (sleeper) peer should always cause
    # a `ContextCancelled` raise in this streaming actor.
    except ContextCancelled as _ctxc:
        ctxc = _ctxc

        # print("TRYING TO ENTER PAUSSE!!!")
        # await tractor.pause(shield=True)
        re: ContextCancelled = peer_ctx._remote_error

        # XXX YES XXX, remote error should be unpacked only once!
        assert (
            re
            is
            peer_ctx.maybe_error
            is
            ctxc
            is
            peer_ctx._local_error
        )
        # NOTE: these errors should all match!
        #   ------ - ------
        # XXX [2024-05-03] XXX
        #   ------ - ------
        # broke this due to a re-raise inside `.msg._ops.drain_to_final_msg()`
        # where the `Error()` msg was directly raising the ctxc
        # instead of just returning up to the caller inside
        # `Context.return()` which would results in a diff instance of
        # the same remote error bubbling out above vs what was
        # already unpacked and set inside `Context.
        assert (
            peer_ctx._remote_error.msgdata
            ==
            ctxc.msgdata
        )
        # ^-XXX-^ notice the data is of course the exact same.. so
        # the above larger assert makes sense to also always be true!

        # XXX YES XXX, bc should be exact same msg instances
        assert peer_ctx._remote_error._ipc_msg is ctxc._ipc_msg

        # XXX NO XXX, bc new one always created for property accesss
        assert peer_ctx._remote_error.ipc_msg != ctxc.ipc_msg

        # the peer ctx is the canceller even though it's canceller
        # is the "canceller" XD
        assert peer_name in peer_ctx.canceller

        assert "canceller" in ctxc.canceller

        # caller peer should not be the cancel requester
        assert not ctx.cancel_called
        assert not ctx.cancel_acked

        # XXX can NEVER BE TRUE since `._invoke` only
        # sets this AFTER the nursery block this task
        # was started in, exits.
        assert not ctx._scope.cancelled_caught

        # we never requested cancellation, it was the 'canceller'
        # peer.
        assert not peer_ctx.cancel_called
        assert not peer_ctx.cancel_acked

        # the `.open_context()` exit definitely caught
        # a cancellation in the internal `Context._scope` since
        # likely the runtime called `_deliver_msg()` after
        # receiving the remote error from the streaming task.
        assert not peer_ctx._scope.cancelled_caught

        # TODO / NOTE `.canceller` won't have been set yet
        # here because that machinery is inside
        # `Portal.open_context().__aexit__()` BUT, if we had
        # a way to know immediately (from the last
        # checkpoint) that cancellation was due to
        # a remote, we COULD assert this here..see,
        # https://github.com/goodboy/tractor/issues/368
        #
        # await tractor.pause()
        # assert 'canceller' in ctx.canceller

        # root/parent actor task should NEVER HAVE cancelled us!
        assert not ctx.canceller

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
    ids=lambda item: f'rte_during_ctxerr={item}',
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
            root: Actor = current_actor()

            try:
                async with (
                    sleeper.open_context(
                        open_stream_then_sleep_forever,
                        expect_ctxc=True,
                    ) as (sleeper_ctx, sent),

                    just_caller.open_context(
                        stream_from_peer,
                        debug_mode=debug_mode,
                    ) as (caller_ctx, sent),

                    canceller.open_context(
                        sleep_a_bit_then_cancel_peer,
                    ) as (canceller_ctx, sent),

                ):
                    ctxs: dict[str, Context] = {
                        'sleeper': sleeper_ctx,
                        'caller': caller_ctx,
                        'canceller': canceller_ctx,
                    }

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
                    except ContextCancelled as _ctxc:
                        ctxc = _ctxc
                        print(
                            'CAUGHT REMOTE CONTEXT CANCEL\n\n'
                            f'{ctxc}\n'
                        )

                        # canceller and caller peers should not
                        # have been remotely cancelled.
                        assert canceller_ctx.canceller is None
                        assert caller_ctx.canceller is None

                        # we were not the actor, our peer was
                        assert not sleeper_ctx.cancel_acked

                        assert ctxc.canceller[0] == 'canceller'

                        # XXX NOTE XXX: since THIS `ContextCancelled`
                        # HAS NOT YET bubbled up to the
                        # `sleeper.open_context().__aexit__()` this
                        # value is not yet set, however outside this
                        # block it should be.
                        assert not sleeper_ctx._scope.cancelled_caught

                        # CASE_1: error-during-ctxc-handling,
                        if error_during_ctxerr_handling:
                            raise RuntimeError('Simulated RTE re-raise during ctxc handling')

                        # CASE_2: standard teardown inside in `.open_context()` block
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
                # - `.cancel_called` (bool of whether this side
                #    requested)
                # - `.cancel_acked` (bool of whether a ctxc
                #   response was received due to cancel req).
                # - `.maybe_error` (highest prio error to raise
                #    locally)
                # - `.outcome` (final error or result value)
                # - `.canceller` (uid of cancel-causing actor-task)
                # - `._remote_error` (any `RemoteActorError`
                #    instance from other side of context)
                # - `._local_error` (any error caught inside the
                #   `.open_context()` block).
                #
                # XXX: Deprecated and internal only
                # - `.cancelled_caught` (maps to nursery cs)
                #  - now just use `._scope.cancelled_caught`
                #    since it maps to the internal (maps to nursery cs)
                #
                # TODO: are we really planning to use this tho?
                # - `._cancel_msg` (any msg that caused the
                #    cancel)

                # CASE_1: error-during-ctxc-handling,
                # - far end cancels due to peer 'canceller',
                # - `ContextCancelled` relayed to this scope,
                # - inside `.open_context()` ctxc is caught and
                #   a rte raised instead
                #
                # => block should raise the rte but all peers
                #   should be cancelled by US.
                #
                if error_during_ctxerr_handling:
                    print(f'loc_err: {_loc_err}\n')
                    assert isinstance(loc_err, RuntimeError)

                    # since we do a rte reraise above, the
                    # `.open_context()` error handling should have
                    # raised a local rte, thus the internal
                    # `.open_context()` enterer task's
                    # cancel-scope should have raised the RTE, NOT
                    # a `trio.Cancelled` due to a local
                    # `._scope.cancel()` call.
                    assert not sleeper_ctx._scope.cancelled_caught
                    # assert sleeper_ctx._local_error is _loc_err
                    # assert sleeper_ctx._local_error is _loc_err
                    assert not (
                        loc_err
                        is sleeper_ctx.maybe_error
                        is sleeper_ctx.outcome
                        is sleeper_ctx._remote_error
                    )

                    # NOTE: this root actor task should have
                    # called `Context.cancel()` on the
                    # `.__aexit__()` to every opened ctx.
                    for name, ctx in ctxs.items():

                        # this root actor task should have
                        # cancelled all opened contexts except the
                        # sleeper which is obvi by the "canceller"
                        # peer.
                        re = ctx._remote_error
                        le = ctx._local_error

                        assert ctx.cancel_called

                        if ctx is sleeper_ctx:
                            assert 'canceller' in re.canceller
                            assert 'sleeper' in ctx.canceller

                        if ctx is canceller_ctx:
                            assert (
                                re.canceller
                                ==
                                root.uid
                            )

                        else:  # the other 2 ctxs
                            assert (
                                isinstance(re, ContextCancelled)
                                and (
                                    re.canceller
                                    ==
                                    canceller.channel.uid
                                )
                            )

                    # since the sleeper errors while handling a
                    # peer-cancelled (by ctxc) scenario, we expect
                    # that the `.open_context()` block DOES call
                    # `.cancel() (despite in this test case it
                    # being unecessary).
                    assert (
                        sleeper_ctx.cancel_called
                        and
                        not sleeper_ctx.cancel_acked
                    )

                # CASE_2: standard teardown inside in `.open_context()` block
                # - far end cancels due to peer 'canceller',
                # - `ContextCancelled` relayed to this scope and
                #   raised locally without any raise-during-handle,
                #
                # => inside `.open_context()` ctxc is raised and
                #   propagated
                #
                else:
                    # since sleeper_ctx.result() IS called above
                    # we should have (silently) absorbed the
                    # corresponding `ContextCancelled` for it and
                    # `._scope.cancel()` should never have been
                    # called.
                    assert not sleeper_ctx._scope.cancelled_caught

                    assert isinstance(loc_err, ContextCancelled)

                    # the received remote error's `.canceller`
                    # will of course be the "canceller" actor BUT
                    # the canceller set on the local handle to
                    # `sleeper_ctx` will be the "sleeper" uid
                    # since it's the actor that relayed us the
                    # error which was **caused** by the
                    # "canceller".
                    assert 'sleeper' in sleeper_ctx.canceller
                    assert 'canceller' == loc_err.canceller[0]

                    # the sleeper's remote error is the error bubbled
                    # out of the context-stack above!
                    final_err = sleeper_ctx.outcome
                    assert (
                        final_err is loc_err
                        is sleeper_ctx.maybe_error
                        is sleeper_ctx._remote_error
                    )

                    for name, ctx in ctxs.items():

                        re: BaseException|None = ctx._remote_error
                        le: BaseException|None = ctx._local_error
                        err = ctx.maybe_error
                        out = ctx.outcome

                        # every ctx should error!
                        assert out is err

                        # the recorded local erro should always be
                        # the same as the one raised by the
                        # `sleeper_ctx.result()` call
                        assert (
                            le
                            and
                            le is loc_err
                        )

                        # root doesn't cancel sleeper since it's
                        # cancelled by its peer.
                        if ctx is sleeper_ctx:
                            assert re
                            assert (
                                ctx._remote_error
                                is ctx.maybe_error
                                is ctx.outcome
                                is ctx._local_error
                            )

                            assert not ctx.cancel_called
                            assert not ctx.cancel_acked

                            # since sleeper_ctx.result() IS called
                            # above we should have (silently)
                            # absorbed the corresponding
                            # `ContextCancelled` for it and thus
                            # the logic inside `.cancelled_caught`
                            # should trigger!
                            assert not ctx._scope.cancelled_caught

                        elif ctx in (
                            caller_ctx,
                            canceller_ctx,
                        ):

                            assert not ctx._remote_error

                            # neither of the `caller/canceller_ctx` should
                            # have called `ctx.cancel()` bc the
                            # canceller's task internally issues
                            # a `Portal.cancel_actor()` to the
                            # sleeper and thus never should call
                            # `ctx.cancel()` per say UNLESS the
                            # sleeper's `.result()` call above
                            # ctxc exception results in the
                            # canceller's
                            # `.open_context().__aexit__()` error
                            # handling to kick in BEFORE a remote
                            # error is delivered - which since
                            # we're asserting what we are above,
                            # that should normally be the case
                            # right?
                            #
                            assert not ctx.cancel_called
                            #
                            # assert ctx.cancel_called
                            # orig ^

                            # TODO: figure out the details of this..?
                            # if you look the `._local_error` here
                            # is a multi of ctxc + 2 Cancelleds?
                            # assert not ctx._scope.cancelled_caught

                            assert (
                                not ctx.cancel_called
                                and not ctx.cancel_acked
                            )
                            assert not ctx._scope.cancelled_caught

                        # elif ctx is canceller_ctx:
                        #     assert not ctx._remote_error

                            # XXX NOTE XXX: ONLY the canceller
                            # will get a self-cancelled outcome
                            # whilst everyone else gets
                            # a peer-caused cancellation!
                            #
                            # TODO: really we should avoid calling
                            # .cancel() whenever an interpeer
                            # cancel takes place since each
                            # reception of a ctxc

                        else:
                            pytest.fail(
                                'Uhh wut ctx is this?\n'
                                f'{ctx}\n'
                            )

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
                    assert not (
                        sleeper_ctx.cancel_called
                        or
                        sleeper_ctx.cancel_acked
                    )

                # XXX NOTE XXX: and see matching comment above but,
                # the `._scope` is only set by `trio` AFTER the
                # `.open_context()` block has exited and should be
                # set in both outcomes including the case where
                # ctx-cancel handling itself errors.
                assert not sleeper_ctx._scope.cancelled_caught
                assert _loc_err is sleeper_ctx._local_error
                assert (
                    sleeper_ctx.outcome
                    is sleeper_ctx.maybe_error
                    is sleeper_ctx._remote_error
                )

                raise  # always to ensure teardown

    if error_during_ctxerr_handling:
        with pytest.raises(RuntimeError) as excinfo:
            trio.run(main)
    else:

        with pytest.raises(ContextCancelled) as excinfo:
            trio.run(main)

        assert excinfo.value.boxed_type == ContextCancelled
        assert excinfo.value.canceller[0] == 'canceller'


@tractor.context
async def basic_echo_server(
    ctx: Context,
    peer_name: str = 'wittle_bruv',

    err_after: int|None = None,

) -> None:
    '''
    Just the simplest `MsgStream` echo server which resays what
    you told it but with its uid in front ;)

    '''
    actor: Actor = tractor.current_actor()
    uid: tuple = actor.uid
    await ctx.started(uid)
    async with ctx.open_stream() as ipc:
        async for msg in ipc:

            # repack msg pair with our uid
            # as first element.
            (
                client_uid,
                i,
            ) = msg
            resp: tuple = (
                uid,
                i,
            )
            # OOF! looks like my runtime-error is causing a lockup
            # assert 0
            await ipc.send(resp)

            if (
                err_after
                and i > err_after
            ):
                raise RuntimeError(
                    f'Simulated error in `{peer_name}`'
                )


@tractor.context
async def serve_subactors(
    ctx: Context,
    peer_name: str,
    debug_mode: bool,

) -> None:
    async with open_nursery() as an:

        # sanity
        assert tractor._state.debug_mode() == debug_mode

        await ctx.started(peer_name)
        async with ctx.open_stream() as ipc:
            async for msg in ipc:
                peer_name: str = msg
                peer: Portal = await an.start_actor(
                    name=peer_name,
                    enable_modules=[__name__],
                )
                print(
                    'Spawning new subactor\n'
                    f'{peer_name}\n'
                    f'|_{peer}\n'
                )
                await ipc.send((
                    peer.chan.uid,
                    peer.chan.raddr.unwrap(),
                ))

        print('Spawner exiting spawn serve loop!')


@tractor.context
async def client_req_subactor(
    ctx: Context,
    peer_name: str,
    debug_mode: bool,

    # used to simulate a user causing an error to be raised
    # directly in thread (like a KBI) to better replicate the
    # case where a `modden` CLI client would hang afer requesting
    # a `Context.cancel()` to `bigd`'s wks spawner.
    reraise_on_cancel: str|None = None,
    sub_err_after: int|None = None,

) -> None:
    # sanity
    if debug_mode:
        assert tractor._state.debug_mode()

    # TODO: other cases to do with sub lifetimes:
    # -[ ] test that we can have the server spawn a sub
    #   that lives longer then ctx with this client.
    # -[ ] test that

    # open ctx with peer spawn server and ask it to spawn a little
    # bro which we'll then connect and stream with.
    async with (
        tractor.find_actor(
            name='spawn_server',
            raise_on_none=True,

            # TODO: we should be isolating this from other runs!
            # => ideally so we can eventually use something like
            # `pytest-xdist` Bo
            # registry_addrs=bigd._reg_addrs,
        ) as spawner,

        spawner.open_context(
            serve_subactors,
            peer_name=peer_name,
            debug_mode=debug_mode,
        ) as (spawner_ctx, first),
    ):
        assert first == peer_name
        await ctx.started(
            'yup i had brudder',
        )

        async with spawner_ctx.open_stream() as reqs:

            # send single spawn request to the server
            await reqs.send(peer_name)
            with trio.fail_after(3):
                (
                    sub_uid,
                    sub_raddr,
                ) = await reqs.receive()


            await tell_little_bro(
                actor_name=sub_uid[0],
                caller='client',
                err_after=sub_err_after,
            )

            # TODO: test different scope-layers of
            # cancellation?
            # with trio.CancelScope() as cs:
            try:
                await trio.sleep_forever()

            # TODO: would be super nice to have a special injected
            # cancel type here (maybe just our ctxc) but using
            # some native mechanism in `trio` :p
            except trio.Cancelled as err:
                _err = err
                if reraise_on_cancel:
                    errtype = globals()['__builtins__'][reraise_on_cancel]
                    assert errtype
                    to_reraise: BaseException = errtype()
                    print(f'client re-raising on cancel: {repr(to_reraise)}')
                    raise err

                raise

            # if cs.cancelled_caught:
            #     print('client handling expected KBI!')
            #     await ctx.
            #     await trio.sleep(
            #     await tractor.pause()
            #     await spawner_ctx.cancel()

            # cancel spawned sub-actor directly?
            # await sub_ctx.cancel()

            # maybe cancel runtime?
            # await sub.cancel_actor()


async def tell_little_bro(
    actor_name: str,

    caller: str = '',
    err_after: int|None = None,
):
    # contact target actor, do a stream dialog.
    async with (
        tractor.wait_for_actor(
            name=actor_name
        ) as lb,
        lb.open_context(
            basic_echo_server,

            # XXX proxy any delayed err condition
            err_after=err_after,
        ) as (sub_ctx, first),

        sub_ctx.open_stream() as echo_ipc,
    ):
        actor: Actor = current_actor()
        uid: tuple = actor.uid
        for i in range(100):
            msg: tuple = (
                uid,
                i,
            )
            await echo_ipc.send(msg)
            resp = await echo_ipc.receive()
            print(
                f'{caller} => {actor_name}: {msg}\n'
                f'{caller} <= {actor_name}: {resp}\n'
            )
            (
                sub_uid,
                _i,
            ) = resp
            assert sub_uid != uid
            assert _i == i


@pytest.mark.parametrize(
    'raise_client_error',
    [None, 'KeyboardInterrupt'],
)
@pytest.mark.parametrize(
    'raise_sub_spawn_error_after',
    [None, 50],
)
def test_peer_spawns_and_cancels_service_subactor(
    debug_mode: bool,
    raise_client_error: str,
    reg_addr: tuple[str, int],
    raise_sub_spawn_error_after: int|None,
):
    # NOTE: this tests for the modden `mod wks open piker` bug
    # discovered as part of implementing workspace ctx
    # open-.pause()-ctx.cancel() as part of the CLI..

    # -> start actor-tree (server) that offers sub-actor spawns via
    #   context API
    # -> start another full actor-tree (client) which requests to the first to
    #   spawn over its `@context` ep / api.
    # -> client actor cancels the context and should exit gracefully
    #   and the server's spawned child should cancel and terminate!
    peer_name: str = 'little_bro'

    def check_inner_rte(rae: RemoteActorError):
        '''
        Validate the little_bro's relayed inception!

        '''
        assert rae.boxed_type is RemoteActorError
        assert rae.src_type is RuntimeError
        assert 'client' in rae.relay_uid
        assert peer_name in rae.src_uid

    async def main():
        async with tractor.open_nursery(
            # NOTE: to halt the peer tasks on ctxc, uncomment this.
            debug_mode=debug_mode,
            registry_addrs=[reg_addr],
        ) as an:
            server: Portal = await an.start_actor(
                (server_name := 'spawn_server'),
                enable_modules=[__name__],
            )
            print(f'Spawned `{server_name}`')

            client: Portal = await an.start_actor(
                client_name := 'client',
                enable_modules=[__name__],
            )
            print(f'Spawned `{client_name}`')

            try:
                async with (
                    server.open_context(
                        serve_subactors,
                        peer_name=peer_name,
                        debug_mode=debug_mode,

                    ) as (spawn_ctx, first),

                    client.open_context(
                        client_req_subactor,
                        peer_name=peer_name,
                        debug_mode=debug_mode,
                        reraise_on_cancel=raise_client_error,

                        # trigger for error condition in sub
                        # during streaming.
                        sub_err_after=raise_sub_spawn_error_after,

                    ) as (client_ctx, client_says),
                ):
                    root: Actor = current_actor()
                    spawner_uid: tuple = spawn_ctx.chan.uid
                    print(
                        f'Server says: {first}\n'
                        f'Client says: {client_says}\n'
                    )

                    # attach to client-requested-to-spawn
                    # (grandchild of this root actor) "little_bro"
                    # and ensure we can also use it as an echo
                    # server.
                    sub: Portal
                    async with tractor.wait_for_actor(
                        name=peer_name,
                    ) as sub:
                        assert sub

                    print(
                        'Sub-spawn came online\n'
                        f'portal: {sub}\n'
                        f'.uid: {sub.actor.uid}\n'
                        f'chan.raddr: {sub.chan.raddr}\n'
                    )

                    async with expect_ctxc(
                        yay=raise_sub_spawn_error_after,
                        reraise=False,
                    ):
                        await tell_little_bro(
                            actor_name=peer_name,
                            caller='root',
                        )

                    if not raise_sub_spawn_error_after:

                        # signal client to cancel and maybe raise a KBI
                        await client_ctx.cancel()
                        print(
                            '-> root cancelling client,\n'
                            '-> root checking `client_ctx.result()`,\n'
                            f'-> checking that sub-spawn {peer_name} is down\n'
                        )

                    try:
                        res = await client_ctx.result(hide_tb=False)

                        # in remote (relayed inception) error
                        # case, we should error on the line above!
                        if raise_sub_spawn_error_after:
                            pytest.fail(
                                'Never rxed proxied `RemoteActorError[RuntimeError]` !?'
                            )

                        assert isinstance(res, ContextCancelled)
                        assert client_ctx.cancel_acked
                        assert res.canceller == root.uid

                    except RemoteActorError as rae:
                        _err = rae
                        assert raise_sub_spawn_error_after

                        # since this is a "relayed error" via the client
                        # sub-actor, it is expected to be
                        # a `RemoteActorError` boxing another
                        # `RemoteActorError` otherwise known as
                        #  an "inception" (from `trio`'s parlance)
                        # ((or maybe a "Matryoshka" and/or "matron"
                        # in our own working parlance)) which
                        # contains the source error from the
                        # little_bro: a `RuntimeError`.
                        #
                        check_inner_rte(rae)
                        assert rae.relay_uid == client.chan.uid
                        assert rae.src_uid == sub.chan.uid

                        assert not client_ctx.cancel_acked
                        assert (
                            client_ctx.maybe_error
                            is client_ctx.outcome
                            is rae
                        )
                        raise
                        # await tractor.pause()

                    else:
                        assert not raise_sub_spawn_error_after

                        # cancelling the spawner sub should
                        # transitively cancel it's sub, the little
                        # bruv.
                        print('root cancelling server/client sub-actors')
                        await spawn_ctx.cancel()
                        async with tractor.find_actor(
                            name=peer_name,
                        ) as sub:
                            assert not sub

                    # await server.cancel_actor()

            except RemoteActorError as rae:
                # XXX more-or-less same as above handler
                # this is just making sure the error bubbles out
                # of the 
                _err = rae
                assert raise_sub_spawn_error_after
                raise

            # since we called `.cancel_actor()`, `.cancel_ack`
            # will not be set on the ctx bc `ctx.cancel()` was not
            # called directly fot this confext.
            except ContextCancelled as ctxc:
                _ctxc = ctxc
                print(
                    f'{root.uid} caught ctxc from ctx with {client_ctx.chan.uid}\n'
                    f'{repr(ctxc)}\n'
                )

                if not raise_sub_spawn_error_after:
                    assert ctxc.canceller == root.uid
                else:
                    assert ctxc.canceller == spawner_uid

                assert ctxc is spawn_ctx.outcome
                assert ctxc is spawn_ctx.maybe_error
                raise

            if raise_sub_spawn_error_after:
                pytest.fail(
                    'context block(s) in PARENT never raised?!?'
                )

            if not raise_sub_spawn_error_after:
                # assert spawn_ctx.cancel_acked
                assert spawn_ctx.cancel_acked
                assert client_ctx.cancel_acked

                await client.cancel_actor()
                await server.cancel_actor()

                # WOA WOA WOA! we need this to close..!!!??
                # that's super bad XD

                # TODO: why isn't this working!?!?
                # we're now outside the `.open_context()` block so
                # the internal `Context._scope: CancelScope` should be
                # gracefully "closed" ;)

                # assert spawn_ctx.cancelled_caught

    if raise_sub_spawn_error_after:
        with pytest.raises(RemoteActorError) as excinfo:
            trio.run(main)

        rae: RemoteActorError = excinfo.value
        check_inner_rte(rae)

    else:
        trio.run(main)
