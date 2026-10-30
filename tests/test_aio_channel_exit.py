'''
Inter-loop channel teardown must handle the issued exit signal.

'''
import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
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
    cancel_trio_side: bool,
    suppress: bool,
    expect_aio_side_err_type: str|None,
    greenback_shim: bool,
    suppress_aio_side_tte: bool = False,
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
    - ``cancel_trio_side`` cancels the caller at a Trio checkpoint.
    - ``suppress`` selects graceful or strict exit-signal reporting.
    - ``expect_aio_side_err_type`` is a class name or ``None``.
      Class names travel as RPC payloads; actual exception objects
      remain inside this actor for identity/cause assertions.
    - ``greenback_shim`` wraps the asyncio task without enabling PDB.
    - ``suppress_aio_side_tte`` handles the issued signal and returns
      normally instead of raising it or a distinct cleanup failure.

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

            # Returning here tests successful asyncio-side completion
            # after it handles the trio-side parent's exit signal.
            if suppress_aio_side_tte:
                return

            # Healthy cleanup re-raises the very signal issued by
            # open_channel_from(); suppression uses object identity.
            if expect_aio_side_err_type is None:
                raise signal

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
        Close a subscription while another stays open in the caller.

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
                            if cancel_trio_side:
                                cs.cancel()
                                await trio.lowlevel.checkpoint()

            except Exception as error:
                caught = error

            assert tasks[task_index].done()
            num_signals: int = len(signals)
            expect_num_signals: int = task_index + 1
            task_error: BaseException|None = (
                tasks[task_index].exception()
            )
            assert num_signals == expect_num_signals, (
                f'Expected one issued TTE per asyncio task\n'
                f'task index: {task_index}\n'
                f'expected signals: {expect_num_signals}\n'
                f'recorded signal count: {num_signals}\n'
                f'caught trio-side error: {caught!r}\n'
                f'asyncio task error: {task_error!r}\n'
                f'recorded signals: {signals!r}\n'
            )
            if expect_aio_side_err_type:
                # For TTE, matching the issued signal's type must NOT
                # suppress a distinct asyncio-side exception.
                # Keep its identity AND its original shutdown cause.
                assert caught is failures[task_index]
                assert caught.__cause__ is signals[task_index]

            elif suppress_aio_side_tte:
                assert caught is None, repr(caught)
                assert tasks[task_index].exception() is None

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
            callback_report: str = '\n'.join(
                f'callback {index}:\n'
                f'  message: {context.get("message")!r}\n'
                f'  exception: {context.get("exception")!r}\n'
                f'  handle: {context.get("handle")!r}\n'
                f'  task: {context.get("task")!r}\n'
                f'  future: {context.get("future")!r}'
                for index, context in enumerate(callback_errors)
            )
            assert not callback_errors, (
                f'Unhandled asyncio callbacks after channel exit\n'
                f'cached: {cached}\n'
                f'cancel_trio_side: {cancel_trio_side}\n'
                f'suppress_graceful_exits: {suppress}\n'
                f'greenback_shim: {greenback_shim}\n'
                f'{callback_report}\n'
            )
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize(
    'greenback_shim',
    [False, True],
    ids=['native', 'greenback'],
)
@pytest.mark.parametrize(
    'infect_root',
    [False, True],
    ids=['infected-child', 'infected-root'],
)
@pytest.mark.parametrize(
    'cached,cancel_trio_side,suppress,expect_aio_side_err_type,'
    'suppress_aio_side_tte',
    [
        # Expected TTE ack: direct use, last cached caller
        # exit, and cancellation of that last cached caller.
        pytest.param(
            False, False, True, None, False,
            id='direct-close',
        ),
        pytest.param(
            True, False, True, None, False,
            id='last-cache-user',
        ),
        pytest.param(
            True, True, True, None, False,
            id='cancelled-last-cache-user',
        ),
        # Strict reporting must preserve the issued TTE's identity.
        pytest.param(
            True, False, False, None, False,
            id='explicit-exit-reporting',
        ),
        # New asyncio-side cleanup failures must escape suppression,
        # even when the exception class matches the issued signal.
        pytest.param(
            True, False, True, 'RuntimeError', False,
            id='independent-cleanup-error',
        ),
        pytest.param(
            True, False, True, 'TrioTaskExited', False,
            id='independent-same-type-error',
        ),
        # The asyncio task may handle the TTE and finish normally.
        pytest.param(
            False, False, True, None, True,
            id='direct-suppress-tte-and-return',
        ),
        pytest.param(
            True, False, True, None, True,
            id='cached-suppress-tte-and-return',
        ),
        # Strict trio-side policy must not manufacture a task error
        # when the asyncio-side task handled its signal successfully.
        pytest.param(
            False, False, False, None, True,
            id='strict-direct-suppress-tte-and-return',
        ),
        pytest.param(
            True, False, False, None, True,
            id='strict-cached-suppress-tte-and-return',
        ),
    ],
)
def test_channel_owner_exit(
    reg_addr: tuple[str, int],
    cached: bool,
    cancel_trio_side: bool,
    suppress: bool,
    expect_aio_side_err_type: str|None,
    greenback_shim: bool,
    infect_root: bool,
    suppress_aio_side_tte: bool,
) -> None:
    '''
    Exercise trio-side channel exit in an infected root or child.

    Regression:
    - ``open_channel_from()`` signalled the running asyncio task when
      its trio-side parent task left the channel context.
    - Raising that issued ``TrioTaskExited`` escaped default graceful
      suppression and was also raised from the asyncio done callback.

    Proof boundary:
    - Infected-child cases invoke ``exercise_trio_side_exit`` by TCP
      RPC. Infected-root cases call it directly in the guest Trio run
      hosted by asyncio, with the root actor runtime open.
    - Both use the actual channel API and broadcast subscriptions;
      cached cases additionally wrap it in ``maybe_open_context()``.
    - Sequential repeats check continued actor usability after prior
      asyncio tasks finish; the child path also checks RPC usability.
    - Strict reporting and independent faults retain their identity;
      broad exception-type suppression cannot satisfy the matrix.
    - The deadline bounds hangs; no live broker or debugger is used.

    Downstream reproduction:
    Piker encountered this during quote-task restart after a farm
    reset, using a cached channel. This test needs neither that
    application nor caching in its direct-channel case.

    '''
    if not infect_root:
        async def main() -> None:
            '''
            Run the selected case directly in a root or via child
            RPCs.

            - reg_addr isolates the runtime's TCP registry.
            - Each call finishes before the next starts.
            - The child path explicitly cancels the actor after its
              RPCs; completing an RPC does not end the actor's
              lifetime.
            - A delayed done-callback failure could terminate the
              child after an RPC body finishes. Another RPC checks
              usability; the endpoint also inspects callback
              exceptions directly.

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
                            cancel_trio_side=cancel_trio_side,
                            suppress=suppress,
                            expect_aio_side_err_type=(
                                expect_aio_side_err_type
                            ),
                            greenback_shim=greenback_shim,
                            suppress_aio_side_tte=suppress_aio_side_tte,
                        )

                    await portal.cancel_actor()

        trio.run(main)

    else:  # infected-root case
        async def infected_root_main():
            '''
            Same as `main` above except invoke
            `exercise_trio_side_exit` in-task with no subactor spawn.

            '''
            # run_as_asyncio_guest() supplies the host asyncio loop.
            # No RPC/child actor is involved: call the same endpoint
            # directly with an infected root runtime open.
            with trio.fail_after(_actor_test_timeout_s):
                async with tractor.open_root_actor(
                    registry_addrs=[reg_addr],
                ):
                    for _ in range(_rpc_repetitions):
                        await exercise_trio_side_exit(
                            cached=cached,
                            cancel_trio_side=cancel_trio_side,
                            suppress=suppress,
                            expect_aio_side_err_type=(
                                expect_aio_side_err_type
                            ),
                            greenback_shim=greenback_shim,
                            suppress_aio_side_tte=(
                                suppress_aio_side_tte
                            ),
                        )

        to_asyncio.run_as_asyncio_guest(
            trio_main=infected_root_main,
        )


async def suppress_tte_and_block(marker: str) -> None:
    '''
    Leave channel teardown waiting on a task that handled its TTE.

    ``marker`` is a test-owned file shared with the parent process.
    Write it ONLY after receiving the issued signal, so cancellation
    of the actor nursery cannot race ahead of the blocked teardown.

    '''
    async def block_until_trio_side_exit(
        chan: to_asyncio.LinkedTaskChannel,
    ) -> None:
        '''
        Handle the issued TTE but keep the asyncio task running.

        Capture marker to acknowledge that the hang precondition was
        reached. Ordinary later asyncio cancellation remains enabled;
        the assertion is actor termination, not mandatory hard-kill.

        '''
        chan.started_nowait()
        try:
            await asyncio.Event().wait()

        except to_asyncio.TrioTaskExited as signal:
            # Publish the marker only for this channel's issued TTE,
            # after trio-side context exit has actually begun.
            assert signal is chan._aio_owner_exit
            assert chan._trio_exited
            assert chan._aio_task is asyncio.current_task()
            assert not chan._aio_task.done()
            Path(marker).write_text(str(os.getpid()))
            await asyncio.Event().wait()

    chan: to_asyncio.LinkedTaskChannel
    async with to_asyncio.open_channel_from(
        block_until_trio_side_exit,
    ) as (chan, first):
        assert first is None
        assert chan._aio_task is not None
        assert not chan._aio_task.done()
        assert not chan._trio_exited

        # Leaving this body immediately sends TTE. Context exit then
        # waits for the asyncio task, which handles TTE and blocks
        # again; the RPC cannot finish without external cancellation.


async def cancel_an_with_blocked_aio(
    marker: str,
    host: str,
    port: int,
) -> None:
    '''
    Cancel the actor nursery after its child suppresses channel TTE.

    Called in a separate parent process by the watchdog test below.
    ActorNursery.cancel() must terminate the child and nursery exit
    must join it. A deadline in the pytest process detects failure
    independently of either actor's event loop.

    '''
    async def invoke_blocked_rpc() -> None:
        '''
        Keep one RPC pending while the parent cancels its actor tree.

        The parent joins this `trio` task through `tn` after
        `an.cancel()`.

        '''
        await portal.run(
            suppress_tte_and_block,
            marker=marker,
        )

    an: tractor.ActorNursery
    async with tractor.open_nursery(
        registry_addrs=[(host, port)],
        debug_mode=False,
    ) as an:
        portal: tractor.Portal = await an.start_actor(
            'blocked_aio_channel',
            enable_modules=[__name__],
            infect_asyncio=True,
            debug_mode=False,
        )
        # Retain the runtime's process handle across nursery cleanup;
        # successful cancel-RPC delivery alone does not prove exit.
        child, = an._children.values()
        proc: trio.Process = child[1]
        tn: trio.Nursery
        async with trio.open_nursery() as tn:
            tn.start_soon(invoke_blocked_rpc)
            while not Path(marker).exists():
                await trio.sleep(0.01)

            await an.cancel()
            tn.cancel_scope.cancel()

    assert proc.poll() is not None, (
        'ActorNursery exited without joining the blocked child'
    )


def test_cancel_an_reaps_blocked_aio(
    tmp_path: Path,
    reg_addr: tuple[str, int],
    _reap_watchdog_s: float = 30,
) -> None:
    '''
    Nursery cancellation must terminate a TTE-suppressing child.

    - Start a dedicated parent process with its own process group.
    - Wait inside that process for the child's TTE receipt marker,
      then call an.cancel() and check its process handle after exit.
    - Expected termination uses Tractor's own cancellation/reaping.
      Watchdog rescue kills this test's group and FAILS the test.
      It cannot count as evidence of successful runtime teardown.

    '''
    if os.name != 'posix':
        pytest.skip('The watchdog uses POSIX process groups')

    marker: Path = tmp_path / 'tte-suppressed'
    code: str = (
        'import sys, trio; '
        'from tests.test_aio_channel_exit '
        'import cancel_an_with_blocked_aio; '
        'trio.run(cancel_an_with_blocked_aio, '
        'sys.argv[1], sys.argv[2], int(sys.argv[3]))'
    )
    parent: subprocess.Popen = subprocess.Popen(
        [
            sys.executable,
            '-c',
            code,
            str(marker),
            reg_addr[0],
            str(reg_addr[1]),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = parent.communicate(timeout=_reap_watchdog_s)

    except subprocess.TimeoutExpired:
        os.killpg(parent.pid, signal.SIGKILL)
        output, _ = parent.communicate()
        pytest.fail(
            f'Watchdog rescued blocked actor-nursery teardown\n'
            f'TTE received: {marker.exists()}\n'
            f'{output}'
        )

    assert marker.exists(), f'Child never received TTE\n{output}'
    assert parent.returncode == 0, (
        f'Blocked-child cancellation failed\n{output}'
    )
