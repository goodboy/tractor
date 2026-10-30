'''
Inter-loop channel teardown must handle the issued exit signal.

'''
import asyncio
from contextlib import asynccontextmanager as acm
from collections.abc import AsyncIterator

import pytest
import tractor
from tractor import (
    to_asyncio,
    trionics,
)
import trio


_num_tasks: int = 2
_rpc_repetitions: int = 2
_actor_test_timeout_s: float = 10


async def exercise_trio_side_exit(
    cached: bool,
    cancelled: bool,
    suppress: bool,
    expect_aio_side_err_type: str|None,
    greenback_shim: bool,
) -> None:
    '''
    Exit direct and cached inter-loop channels in an infected actor.

    Failure mechanism:
    - The trio-side parent task exits with asyncio still running.
    - ``open_channel_from()`` signals the asyncio task to exit.
    - Previously, that signal escaped channel teardown and became
      an unhandled exception from the asyncio done callback.

    Cases:
    - ``cached`` wraps the channel API in ``maybe_open_context()``.
    - ``cancelled`` cancels the last consumer at a Trio checkpoint.
    - ``suppress`` selects graceful or strict exit-signal reporting.
    - ``expect_aio_side_err_type`` is a class name or ``None``.
      Class names travel as RPC payloads; actual exception objects
      remain inside this actor for identity/cause assertions.
    - ``greenback_shim`` wraps the asyncio task without enabling PDB.

    Ordering and proof:
    - ``tn.start()`` waits for a sibling subscription; ``release``
      lets it retire while the original subscriber remains alive.
    - The asyncio task must finish before channel-context exit ends.
    - Identity assertions distinguish our signal from cleanup faults.
    - Each of ``_num_tasks`` iterations opens a fresh asyncio task.
    - A host-loop callback sets ``drained`` before callback-failure
      inspection, independently of actor/RPC survival.

    '''
    if expect_aio_side_err_type not in {
        None,
        'RuntimeError',
        'TrioTaskExited',
    }:
        raise ValueError(
            f'Unknown aio error case: {expect_aio_side_err_type!r}'
        )

    # Keep the actual objects: equality/type checks cannot prove that
    # teardown distinguishes an issued signal from a new same-type
    # asyncio failure. Task/signal/failure entries follow iteration
    # order; callback_errors records host-loop exception contexts.
    callback_errors: list[dict] = []
    tasks: list[asyncio.Task] = []
    failures: list[Exception] = []
    signals: list[BaseException] = []
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda loop, context: callback_errors.append(context),
    )

    async def block_until_trio_side_exit(
        chan: to_asyncio.LinkedTaskChannel,
    ) -> None:
        '''
        Block until the channel API signals trio-side parent exit.

        - Record task identity, finish startup, send a channel value,
          then await an event which is never set.
        - Record the received ``TrioTaskExited`` object and either
          re-raise it or raise the selected distinct cleanup failure.
        - ``tasks``, ``signals`` and ``failures`` retain the objects
          checked after teardown; ``greenback_shim`` selects whether
          this coroutine initializes a Greenback portal.

        '''
        task: asyncio.Task|None = asyncio.current_task()
        assert task is not None
        tasks.append(task)
        if greenback_shim:
            import greenback
            await greenback.ensure_portal()

        chan.started_nowait('ready')
        chan.send_nowait('tick')
        try:
            await asyncio.Event().wait()

        except to_asyncio.TrioTaskExited as signal:
            signals.append(signal)

            # Healthy cleanup re-raises the very signal issued by
            # open_channel_from(); suppression uses object identity.
            if expect_aio_side_err_type is None:
                raise

            failure: RuntimeError|to_asyncio.TrioTaskExited
            if expect_aio_side_err_type == 'RuntimeError':
                # Fail during cleanup after receiving the channel
                # owner's exit notification.
                failure = RuntimeError(
                    'asyncio cleanup failed after trio-side exit',
                )

            elif expect_aio_side_err_type == 'TrioTaskExited':
                # Deliberately use the SAME TYPE as the issued signal
                # but a DIFFERENT INSTANCE. A blanket TrioTaskExited
                # handler would hide this distinct asyncio error.
                failure = to_asyncio.TrioTaskExited(
                    'new asyncio TTE, not the issued exit signal',
                )

            # Retain both objects: assertions below require the new
            # failure to propagate with the issued signal as cause.
            # "Independent" means distinct from the issued signal;
            # the cleanup failure still follows that notification.
            failures.append(failure)
            raise failure from signal

    @acm
    async def open_raw_aio_chan() -> AsyncIterator[
        to_asyncio.LinkedTaskChannel
    ]:
        '''
        Open the blocking asyncio task through open_channel_from().

        - Run ``block_until_trio_side_exit`` using the actual
          ``to_asyncio.open_channel_from()`` API.
        - Forward captured ``suppress`` unchanged so strict reporting
          and graceful suppression exercise the same ownership path.
        - Yield after the asyncio task's startup handshake. Context
          exit signals that task and waits for it to finish.

        '''
        chan: to_asyncio.LinkedTaskChannel
        first: str
        async with to_asyncio.open_channel_from(
            block_until_trio_side_exit,
            suppress_graceful_exits=suppress,
        ) as (chan, first):
            assert first == 'ready'
            yield chan

    @acm
    async def open_maybe_cached_aio_chan() -> AsyncIterator[
        to_asyncio.LinkedTaskChannel
    ]:
        '''
        Optionally cache the channel with ``maybe_open_context()``.

        With captured ``cached=True``, overlapping calling trio.Tasks
        receive the same channel instance. Otherwise each call enters
        ``open_raw_aio_chan()`` directly.

        '''
        chan: to_asyncio.LinkedTaskChannel
        if cached:
            async with trionics.maybe_open_context(
                acm_func=open_raw_aio_chan,
                # Same factory/key means both calling trio.Tasks get
                # the same underlying channel. Reuse it on subsequent
                # iterations to check retirement and reallocation.
                key='aio_chan',
            ) as (_, chan):
                yield chan

        else:
            async with open_raw_aio_chan() as chan:
                yield chan

    async def subscribe_cached_chan(
        expected: to_asyncio.LinkedTaskChannel,
        release: trio.Event,
        *,
        task_status: trio.TaskStatus[None] = (
            trio.TASK_STATUS_IGNORED
        ),
    ) -> None:
        '''
        Close one cached-channel subscription while another remains.

        - Enter ``open_maybe_cached_aio_chan`` in another trio.Task.
        - Require ``expected`` identity to prove actual cache reuse.
        - Report started only once the broadcast subscription exists.
        - Wait for ``release`` before exiting; the caller joins this
          task before checking that the asyncio task is still alive.

        '''
        other: to_asyncio.LinkedTaskChannel
        async with open_maybe_cached_aio_chan() as other:
            assert other is expected
            async with other.subscribe():
                task_status.started()
                await release.wait()

    try:
        task_index: int
        for task_index in range(_num_tasks):
            caught: BaseException|None = None
            try:
                cs: trio.CancelScope
                chan: to_asyncio.LinkedTaskChannel
                bchan: trionics.BroadcastReceiver
                with trio.CancelScope() as cs:
                    async with open_maybe_cached_aio_chan() as chan:
                        async with chan.subscribe() as bchan:
                            if cached:
                                release: trio.Event = trio.Event()

                                tn: trio.Nursery
                                async with (
                                    trio.open_nursery() as tn
                                ):
                                    await tn.start(
                                        subscribe_cached_chan,
                                        chan,
                                        release,
                                    )
                                    release.set()

                                # The sibling has closed its handles.
                                # Our subscription still retains the
                                # aio task; this is not final exit.
                                assert not tasks[task_index].done()

                            # Consume a real broadcast value before
                            # exit or last-consumer cancellation.
                            assert await bchan.receive() == 'tick'
                            if cancelled:
                                cs.cancel()
                                await trio.lowlevel.checkpoint()

            except Exception as error:
                caught = error

            assert tasks[task_index].done()
            assert len(signals) == task_index + 1, (
                caught,
                tasks[task_index].exception(),
                signals,
            )
            if expect_aio_side_err_type:
                # For TTE, matching the issued signal's type must NOT
                # suppress a distinct asyncio-side exception.
                # Keep its identity AND its original shutdown cause.
                assert caught is failures[task_index]
                assert caught.__cause__ is signals[task_index]

            elif not suppress:
                # Strict reporting returns the exact issued signal,
                # not a new wrapper or an asyncio-created substitute.
                assert caught is signals[task_index]

            else:
                # Default policy absorbs expected owner exit, even
                # when the cached resource's last consumer cancels.
                assert caught is None, repr(caught)

        # Cross the host loop's ready queue before accepting callback
        # cleanliness, independently of actor survival.
        drained: trio.Event = trio.Event()
        loop.call_soon(drained.set)
        await drained.wait()
        if not expect_aio_side_err_type:
            # Expected shutdown must have no unhandled callbacks.
            # Fault cases above test propagation and causes, without
            # asserting a broader callback-cleanliness policy.
            assert not callback_errors, callback_errors
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize(
    'greenback_shim',
    [False, True],
    ids=['native', 'greenback'],
)
@pytest.mark.parametrize(
    'cached,cancelled,suppress,expect_aio_side_err_type',
    [
        # Expected TTE ack: direct use, last cached caller
        # exit, and cancellation of that last cached caller.
        pytest.param(
            False, False, True, None,
            id='direct-close',
        ),
        pytest.param(
            True, False, True, None,
            id='last-cache-user',
        ),
        pytest.param(
            True, True, True, None,
            id='cancelled-last-cache-user',
        ),
        # Strict reporting must preserve the issued TTE's identity.
        pytest.param(
            True, False, False, None,
            id='explicit-exit-reporting',
        ),
        # New asyncio-side cleanup failures must escape suppression,
        # even when the exception class matches the issued signal.
        pytest.param(
            True, False, True, 'RuntimeError',
            id='independent-cleanup-error',
        ),
        pytest.param(
            True, False, True, 'TrioTaskExited',
            id='independent-same-type-error',
        ),
    ],
)
def test_channel_owner_exit(
    reg_addr: tuple[str, int],
    cached: bool,
    cancelled: bool,
    suppress: bool,
    expect_aio_side_err_type: str|None,
    greenback_shim: bool,
) -> None:
    '''
    Channel teardown must leave the infected child actor RPC-capable.

    Regression:
    - ``open_channel_from()`` signalled the running asyncio task when
      its trio-side parent task left the channel context.
    - Raising that issued ``TrioTaskExited`` escaped default graceful
      suppression and was also raised from the asyncio done callback.

    Proof boundary:
    - Real TCP RPC invokes ``exercise_trio_side_exit`` in an infected
      child, using the channel API and its broadcast subscriptions.
      Cached cases additionally wrap it in ``maybe_open_context()``.
    - Repeated RPCs in that same actor prove it remains usable after
      the endpoint's asyncio tasks have finished.
    - Strict reporting and independent faults retain their identity;
      broad exception-type suppression cannot satisfy the matrix.
    - The deadline bounds hangs; no live broker or debugger is used.

    Downstream reproduction:
    Piker encountered this during quote-task restart after a farm
    reset, using a cached channel. This test needs neither that
    application nor caching in its direct-channel case.

    '''
    async def main() -> None:
        '''
        Own the infected child and bound the complete RPC exercise.

        - Capture ``reg_addr`` for the test's isolated TCP registry.
        - Forward the selected case to each RPC in the same child.
        - A callback could fail after the RPC body finishes and then
          terminate the actor. A second RPC checks continued use;
          the callback-drain assertion inside the endpoint separately
          checks the failure directly, without relying on RPC timing.
        - Explicitly cancel the child after the probes: a successful
          RPC does not itself end the actor's service lifetime.

        '''
        an: tractor.ActorNursery
        with trio.fail_after(_actor_test_timeout_s):
            async with tractor.open_nursery(
                registry_addrs=[reg_addr],
            ) as an:
                portal: tractor.Portal = await an.start_actor(
                    'aio_channel_owner',
                    enable_modules=[__name__],
                    infect_asyncio=True,
                )
                # portal.run() waits for this remote task's final
                # result. These RPCs are sequential, not concurrent;
                # only the endpoint's subscription tasks overlap.
                for _ in range(_rpc_repetitions):
                    await portal.run(
                        exercise_trio_side_exit,
                        cached=cached,
                        cancelled=cancelled,
                        suppress=suppress,
                        expect_aio_side_err_type=(
                            expect_aio_side_err_type
                        ),
                        greenback_shim=greenback_shim,
                    )

                await portal.cancel_actor()

    trio.run(main)
