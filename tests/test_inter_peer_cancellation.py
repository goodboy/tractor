'''
Codify the cancellation request semantics in terms
of one remote actor cancelling another.

'''
from contextlib import asynccontextmanager as acm

import pytest
import trio
import tractor
from tractor._exceptions import (
    StreamOverrun,
    ContextCancelled,
)


def test_self_cancel():
    '''
    2 cases:
    - calls `Actor.cancel()` locally in some task
    - calls LocalPortal.cancel_actor()` ?

    '''
    ...


@tractor.context
async def sleep_forever(
    ctx: tractor.Context,
) -> None:
    '''
    Sync the context, open a stream then just sleep.

    '''
    await ctx.started()
    async with ctx.open_stream():
        await trio.sleep_forever()


@acm
async def attach_to_sleep_forever():
    '''
    Cancel a context **before** any underlying error is raised in order
    to trigger a local reception of a ``ContextCancelled`` which **should not**
    be re-raised in the local surrounding ``Context`` *iff* the cancel was
    requested by **this** side of the context.

    '''
    async with tractor.wait_for_actor('sleeper') as p2:
        async with (
            p2.open_context(sleep_forever) as (peer_ctx, first),
            peer_ctx.open_stream(),
        ):
            try:
                yield
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


@tractor.context
async def error_before_started(
    ctx: tractor.Context,
) -> None:
    '''
    This simulates exactly an original bug discovered in:
    https://github.com/pikers/piker/issues/244

    '''
    async with attach_to_sleep_forever():

        # XXX NOTE XXX: THIS sends an UNSERIALIZABLE TYPE which
        # should raise a `TypeError` and **NOT BE SWALLOWED** by
        # the surrounding acm!!?!
        await ctx.started(object())


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
async def sleep_a_bit_then_cancel_sleeper(
    ctx: tractor.Context,
) -> None:
    async with tractor.wait_for_actor('sleeper') as sleeper:
        await ctx.started()
        # await trio.sleep_forever()
        await trio.sleep(3)
    # async with tractor.wait_for_actor('sleeper') as sleeper:
        await sleeper.cancel_actor()


def test_peer_canceller():
    '''
    Verify that a cancellation triggered by a peer (whether in tree
    or not) results in a cancelled error with
    a `ContextCancelled.errorer` matching the requesting actor.

    cases:
    - some arbitrary remote peer cancels via Portal.cancel_actor().
      => all other connected peers should get that cancel requesting peer's
         uid in the ctx-cancelled error msg.

    - peer spawned a sub-actor which (also) spawned a failing task
      which was unhandled and propagated up to the immediate
      parent, the peer to the actor that also spawned a remote task
      task in that same peer-parent.

    - peer cancelled itself - so other peers should
      get errors reflecting that the peer was itself the .canceller?

    - WE cancelled the peer and thus should not see any raised
      `ContextCancelled` as it should be reaped silently?
      => pretty sure `test_context_stream_semantics::test_caller_cancels()`
         already covers this case?

    '''

    async def main():
        async with tractor.open_nursery() as n:
            canceller: tractor.Portal = await n.start_actor(
                'canceller',
                enable_modules=[__name__],
            )
            sleeper: tractor.Portal = await n.start_actor(
                'sleeper',
                enable_modules=[__name__],
            )

            async with (
                sleeper.open_context(
                    sleep_forever,
                ) as (sleeper_ctx, sent),

                canceller.open_context(
                    sleep_a_bit_then_cancel_sleeper,
                ) as (canceller_ctx, sent),
            ):
                # await tractor.pause()
                try:
                    print('PRE CONTEXT RESULT')
                    await sleeper_ctx.result()

                # TODO: not sure why this isn't catching
                # but maybe we need an `ExceptionGroup` and
                # the whole except *errs: thinger in 3.11?
                except (
                    ContextCancelled,
                ) as berr:
                    print('CAUGHT REMOTE CONTEXT CANCEL')

                    # canceller should not have been remotely
                    # cancelled.
                    assert canceller_ctx.cancel_called_remote is None
                    assert sleeper_ctx.canceller == 'canceller'
                    await tractor.pause(shield=True)
                    assert not sleep_ctx.cancelled_caught

                    raise
                else:
                    raise RuntimeError('NEVER RXED EXPECTED `ContextCancelled`')


    with pytest.raises(tractor.ContextCancelled) as excinfo:
        trio.run(main)

    assert excinfo.value.type == ContextCancelled
