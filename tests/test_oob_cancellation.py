'''
Define the details of inter-actor "out-of-band" (OoB) cancel
semantics, that is how cancellation works when a cancel request comes
from the different concurrency (primitive's) "layer" then where the
eventual `trio.Task` actually raises a signal.

'''
from functools import partial
# from contextlib import asynccontextmanager as acm
# import itertools

import pytest
import trio
import tractor
from tractor import (  # typing
    ActorNursery,
    Portal,
    Context,
    # ContextCancelled,
    # RemoteActorError,
)
# from tractor._testing import (
#     tractor_test,
#     expect_ctxc,
# )

# XXX TODO cases:
# - [ ] peer cancelled itself - so other peers should
#   get errors reflecting that the peer was itself the .canceller?

# def test_self_cancel():
#     '''
#     2 cases:
#     - calls `Actor.cancel()` locally in some task
#     - calls LocalPortal.cancel_actor()` ?
#
# things to ensure!
# -[ ] the ctxc raised in a child should ideally show the tb of the
#     underlying `Cancelled` checkpoint, i.e.
#     `raise scope_error from ctxc`?
#
# -[ ] a self-cancelled context, if not allowed to block on
#     `ctx.result()` at some point will hang since the `ctx._scope`
#     is never `.cancel_called`; cases for this include,
#     - an `open_ctx()` which never starteds before being OoB actor
#       cancelled.
#       |_ parent task will be blocked in `.open_context()` for the
#         `Started` msg, and when the OoB ctxc arrives `ctx._scope`
#         will never have been signalled..

#     '''
#     ...

# TODO, sanity test against the case in `/examples/trio/lockacquire_not_unmasked.py`
# but with the `Lock.acquire()` from a `@context` to ensure the
# implicit ignore-case-non-unmasking.
#
# @tractor.context
# async def acquire_actor_global_lock(
#     ctx: tractor.Context,
#     ignore_special_cases: bool,
# ):

#     async with maybe_unmask_excs(
#         ignore_special_cases=ignore_special_cases,
#     ):
#         await ctx.started('locked')

#     # block til cancelled
#     await trio.sleep_forever()


@tractor.context
async def sleep_forever(
    ctx: tractor.Context,
    # ignore_special_cases: bool,
    do_started: bool,
):

    # async with maybe_unmask_excs(
    #     ignore_special_cases=ignore_special_cases,
    # ):
    #     await ctx.started('locked')
    if do_started:
        await ctx.started()

    # block til cancelled
    print('sleepin on child-side..')
    await trio.sleep_forever()


@pytest.mark.parametrize(
    'cancel_ctx',
    [True, False],
)
def test_cancel_ctx_with_parent_side_entered_in_bg_task(
    debug_mode: bool,
    loglevel: str,
    cancel_ctx: bool,
):
    '''
    The most "basic" out-of-band-task self-cancellation case where
    `Portal.open_context()` is entered in a bg task and the
    parent-task (of the containing nursery) calls `Context.cancel()`
    without the child knowing; the `Context._scope` should be
    `.cancel_called` when the IPC ctx's child-side relays
    a `ContextCancelled` with a `.canceller` set to the parent
    actor('s task).

    '''
    async def main():
        with trio.fail_after(
            2 if not debug_mode else 999,
        ):
            an: ActorNursery
            async with (
                tractor.open_nursery(
                    debug_mode=debug_mode,
                    loglevel='devx',
                    enable_stack_on_sig=True,
                ) as an,
                trio.open_nursery() as tn,
            ):
                ptl: Portal = await an.start_actor(
                    'sub',
                    enable_modules=[__name__],
                )

                async def _open_ctx_async(
                    do_started: bool = True,
                    task_status=trio.TASK_STATUS_IGNORED,
                ):
                    # do we expect to never enter the
                    # `.open_context()` below.
                    if not do_started:
                        task_status.started()

                    async with ptl.open_context(
                        sleep_forever,
                        do_started=do_started,
                    ) as (ctx, first):
                        task_status.started(ctx)
                        await trio.sleep_forever()

                # XXX, this is the key OoB part!
                #
                # - start the `.open_context()` in a bg task which
                #   blocks inside the embedded scope-body,
                #
                # -  when we call `Context.cancel()` it **is
                #   not** from the same task which eventually runs
                #   `.__aexit__()`,
                #
                # - since the bg "opener" task will be in
                #   a `trio.sleep_forever()`, it must be interrupted
                #   by the `ContextCancelled` delivered from the
                #   child-side; `Context._scope: CancelScope` MUST
                #   be `.cancel_called`!
                #
                print('ASYNC opening IPC context in subtask..')
                maybe_ctx: Context|None = await tn.start(partial(
                    _open_ctx_async,
                ))

                if (
                    maybe_ctx
                    and
                    cancel_ctx
                ):
                    print('cancelling first IPC ctx!')
                    await maybe_ctx.cancel()

                # XXX, note that despite `maybe_context.cancel()`
                # being called above, it's the parent (bg) task
                # which was originally never interrupted in
                # the `ctx._scope` body due to missing case logic in
                # `ctx._maybe_cancel_and_set_remote_error()`.
                #
                # It didn't matter that the subactor process was
                # already terminated and reaped, nothing was
                # cancelling the ctx-parent task's scope!
                #
                print('cancelling subactor!')
                await ptl.cancel_actor()

                if maybe_ctx:
                    try:
                        await maybe_ctx.wait_for_result()
                    except tractor.ContextCancelled as ctxc:
                        assert not cancel_ctx
                        assert (
                            ctxc.canceller
                            ==
                            tractor.current_actor().aid.uid
                        )
                        # don't re-raise since it'll trigger
                        # an EG from the above tn.

    if cancel_ctx:
        # graceful self-cancel
        trio.run(main)

    else:
        # ctx parent task should see OoB ctxc due to
        # `ptl.cancel_actor()`.
        with pytest.raises(tractor.ContextCancelled) as excinfo:
            trio.run(main)

        'root' in excinfo.value.canceller[0]


# def test_parent_actor_cancels_subactor_with_gt1_ctxs_open_to_it(
#     debug_mode: bool,
#     loglevel: str,
# ):
#     '''
#     Demos OoB cancellation from the perspective of a ctx opened with
#     a child subactor where the parent cancels the child at the "actor
#     layer" using `Portal.cancel_actor()` and thus the
#     `ContextCancelled.canceller` received by the ctx's parent-side
#     task will appear to be a "self cancellation" even though that
#     specific task itself was not cancelled and thus
#     `Context.cancel_called ==False`.
#     '''
                # TODO, do we have an existing implied ctx
                # cancel test like this?
                # with trio.move_on_after(0.5):# as cs:
                #     await _open_ctx_async(
                #         do_started=False,
                #     )


                # in-line ctx scope should definitely raise
                # a ctxc with `.canceller = 'root'`
                # async with ptl.open_context(
                #     sleep_forever,
                #     do_started=True,
                # ) as pair:

