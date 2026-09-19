'''
Synchronous cancellation after the RPC service nursery exits.

'''
from contextlib import contextmanager
from pathlib import Path
import platform
from traceback import format_exc
from uuid import uuid4

import pytest
import trio
from trio.testing import wait_all_tasks_blocked

import tractor
from tractor._testing import tractor_test
from tractor.ipc import _shm


async def start_late_cancel(
    finalizer_path: str,
    close_services: bool,
    shm_key: str,
    cancel_path: str,
) -> dict:
    '''
    Arrange cancellation from outside the RPC service nursery.

    '''
    actor: tractor.Actor = tractor.current_actor()
    root_tn: trio.Nursery = actor._root_tn
    service_tn: trio.Nursery = actor._service_tn
    checked: trio.Event = trio.Event()
    result_path: Path = Path(finalizer_path)

    @contextmanager
    def report_failure():
        '''
        Preserve child failures even after its IPC loop exits.

        '''
        try:
            yield
        except Exception:
            if not result_path.exists():
                result_path.write_text(format_exc())
            # Unwind this fresh child even when the regression
            # leaves its shielded parent-channel loop alive.
            actor._parent_chan_cs.cancel()
            root_tn.cancel_scope.cancel()
            raise

    def finalize() -> None:
        '''
        Prove runtime unwinding reached the lifetime callbacks.

        '''
        with report_failure():
            assert checked.is_set()
            assert root_tn._closed
            assert actor.cancel_complete
            # Completed teardown must not schedule into the closed
            # Actor._root_tn, including repeated finalizer calls.
            assert actor.cancel_soon() is False
            assert actor.cancel_soon() is False
            result_path.write_text('finalized')

    actor.lifetime_stack.callback(finalize)

    # ExitStack is LIFO: allocator destroy/close callbacks must
    # finish before finalize publishes its success marker.
    with report_failure():
        shmarr = _shm.open_shm_ndarray(
            size=4,
            key=shm_key,
            dtype=[('index', 'i8')],
        )

    async def cancel_after_rpc() -> None:
        '''
        Let the setup RPC return before shutting down services.

        '''
        with (
            report_failure(),
            trio.CancelScope(shield=True),
            trio.fail_after(5),
        ):
            # The parent first proves every segment exists and
            # closes its probe handles, then releases this gate.
            while not Path(cancel_path).exists():
                await trio.sleep(0.01)
            await wait_all_tasks_blocked()
            if close_services:
                service_tn.cancel_scope.cancel()
                await wait_all_tasks_blocked()
                assert service_tn._closed

            assert not root_tn._closed
            assert not actor.cancel_complete
            assert actor._parent_chan_cs.shield
            # No checkpoint between calls: the first schedules the
            # root-owned task; the duplicate must not schedule one.
            assert actor.cancel_soon() is True
            assert actor.cancel_soon() is False
            await actor._cancel_complete.wait()
            await wait_all_tasks_blocked()
            assert actor._parent_chan_cs.cancel_called
            assert root_tn.cancel_scope.cancel_called
            assert actor.cancel_soon() is False
            checked.set()

    root_tn.start_soon(cancel_after_rpc)
    return shmarr.token


@pytest.mark.parametrize(
    'infect_asyncio',
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                platform.system() == 'Windows',
                reason='asyncio infection is unsupported on Windows',
            ),
        ),
    ],
)
@pytest.mark.parametrize('close_services', [False, True])
@tractor_test
async def test_cancel_soon_finishes_runtime(
    reg_addr: tuple,
    tmp_path: Path,
    close_services: bool,
    infect_asyncio: bool,
) -> None:
    '''
    Late Actor.cancel_soon must finish teardown after services exit.

    An IB disconnect left Actor._service_tn closed while the root
    nursery and shielded parent-channel loop survived. A late
    synchronous cancellation then raised RuntimeError from
    Nursery.start_soon, preventing the asyncio host from waiting
    for its Trio guest and the actor's lifetime finalizers.

    A fresh child schedules a root-nursery controller. Trio's
    wait_all_tasks_blocked lets its setup RPC finish, then proves
    service-nursery exit before requesting cancellation. The open
    service variant also guards ordinary self-cancellation. Run
    both with native Trio and an asyncio-infected child runtime.

    Repeated requests before the canceller runs, after completion,
    and from a lifetime callback must return False; only the first
    request returns True. A shielded controller checks that
    Actor.cancel actually cancels the
    parent-channel scope and the root nursery. Only runtime exit
    may write the finalizer marker; the parent waits for it before
    leaving its actor nursery, so parent teardown cannot cause a
    false pass. No signals or external services are involved.

    The child allocates a four-row array via open_shm_ndarray.
    A file gate holds cancellation until the parent has opened and
    closed all three data/first/last segments. The allocator's
    lifetime destroy/close callbacks precede the success marker.
    Before exiting its actor nursery, the parent proves all three
    names are gone. No parent lifetime callback owns these segments;
    a finally block cleans only this test's unique names if a
    failure leaks them, after the assertions have already failed.

    '''
    pytest.importorskip('numpy')
    finalizer_path: Path = tmp_path / 'finalized'
    cancel_path: Path = tmp_path / 'cancel'
    shm_key: str = f'cancel_soon_{uuid4().hex}'
    token: _shm.NDToken = _shm._make_token(
        key=shm_key,
        size=4,
        dtype=[('index', 'i8')],
    )
    # Know the exact names even if allocation or the setup RPC
    # fails partway through. _make_token also handles macOS names.
    names: tuple[str, ...] = (
        token.shm_name,
        token.shm_first_index_name,
        token.shm_last_index_name,
    )
    assert len(set(names)) == 3
    # Reject any pre-existing name before entering our cleanup
    # block: a collision would not belong to this test invocation.
    for name in names:
        try:
            probe = _shm.SharedMemory(name, create=False)
        except FileNotFoundError:
            continue
        else:
            probe.close()
            pytest.fail(f'SHM name already exists: {name}')

    try:
        with trio.fail_after(10):
            async with tractor.open_nursery() as an:
                portal: tractor.Portal = await an.start_actor(
                    'late_cancel',
                    enable_modules=[__name__],
                    infect_asyncio=infect_asyncio,
                )
                allocated: dict = await portal.run(
                    start_late_cancel,
                    finalizer_path=str(finalizer_path),
                    close_services=close_services,
                    shm_key=shm_key,
                    cancel_path=str(cancel_path),
                )
                assert _shm.NDToken.from_msg(allocated) == token
                for name in names:
                    probe = _shm.SharedMemory(name, create=False)
                    probe.close()
                cancel_path.touch()

                while not finalizer_path.exists():
                    await trio.sleep(0.01)

                result: str = finalizer_path.read_text()
                assert result == 'finalized', result

                remaining: list[str] = []
                for name in names:
                    try:
                        probe = _shm.SharedMemory(name, create=False)
                    except FileNotFoundError:
                        continue
                    else:
                        probe.close()
                        remaining.append(name)
                assert not remaining, remaining
    finally:
        # No scanning: only this invocation's three unique names.
        # Run after child teardown, never before the absence check.
        for name in names:
            try:
                leaked = _shm.SharedMemory(name, create=False)
            except FileNotFoundError:
                continue
            try:
                leaked.unlink()
            finally:
                leaked.close()
