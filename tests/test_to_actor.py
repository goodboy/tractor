'''
`tractor.to_actor`: one-shot single-remote-task API suite.

Verifies the "spiritual successor" to (and eventual
replacement of) `ActorNursery.run_in_actor()`; see
https://github.com/goodboy/tractor/issues/477

'''
from functools import partial

import pytest
import trio
import tractor
from tractor import (
    RemoteActorError,
    to_actor,
)
from tractor._testing import tractor_test


async def add_one(
    n: int,
) -> int:
    return n + 1


async def raise_value_error() -> None:
    raise ValueError('kaboom')


@tractor_test
async def test_one_shot_in_private_nursery(
    start_method: str,
    debug_mode: bool,
):
    '''
    No `an`/`portal` provided: a private actor-nursery
    is opened (and torn down) scoped to just the call.

    '''
    assert await to_actor.run(
        add_one,
        n=1,
    ) == 2


def test_one_shot_boots_implicit_runtime(
    reg_addr: tuple,
    start_method: str,
    loglevel: str,
):
    '''
    Outside any actor-runtime `to_actor.run()` boots one
    implicitly (just like bare `open_nursery()` usage)
    configured via pass-through `runtime_kwargs`.

    '''
    async def main() -> None:
        assert tractor.current_actor(
            err_on_no_runtime=False,
        ) is None
        result = await to_actor.run(
            add_one,
            n=41,
            runtime_kwargs=dict(
                registry_addrs=[reg_addr],
                start_method=start_method,
                loglevel=loglevel,
            ),
        )
        assert result == 42

    trio.run(main)


@tractor_test
async def test_remote_error_relayed_to_caller_task(
    start_method: str,
    debug_mode: bool,
):
    '''
    A remote task error is raised directly in the
    caller's task as a boxed `RemoteActorError` instead
    of surfacing at actor-nursery teardown as with the
    legacy `.run_in_actor()` API.

    '''
    with pytest.raises(RemoteActorError) as excinfo:
        await to_actor.run(raise_value_error)

    assert excinfo.value.boxed_type is ValueError


@tractor_test
async def test_spawn_from_caller_nursery(
    start_method: str,
    debug_mode: bool,
):
    '''
    Pass a caller-managed `an: ActorNursery` for the spawn.

    Previously `to_actor.run()` treated an actor-runtime cancel ack
    as process reaping, so the call returned while the child monitor
    and its `ActorNursery._children` record remained alive until the
    entire nursery exited. The assertion inside the still-open
    nursery proves child-process joining and record removal now
    complete before the one-shot call returns.

    '''
    async with tractor.open_nursery() as an:
        assert await to_actor.run(
            add_one,
            an=an,
            n=10,
        ) == 11
        assert not an._children


@tractor_test
async def test_cancel_ack_failure_hard_reaps_child(
    monkeypatch: pytest.MonkeyPatch,
    start_method: str,
    debug_mode: bool,
):
    '''
    Escalate a failed cancel acknowledgement and reap the child.

    `Portal.cancel_actor()` can return `False` when its transport is
    already closed without confirming runtime cancellation. The old
    one-shot path ignored that result, released the nursery-wide join
    gate and then waited forever for a still-running process. This
    test forces that exact result without cancelling the actor, caps
    the call to detect the former hang and verifies the child monitor
    removes its `ActorNursery._children` record before returning.

    '''
    async def cancel_without_ack(
        portal: tractor.Portal,
        timeout: float|None = None,
        raise_on_timeout: bool = False,
    ) -> bool:
        assert raise_on_timeout
        return False

    monkeypatch.setattr(
        tractor.Portal,
        'cancel_actor',
        cancel_without_ack,
    )

    async with tractor.open_nursery() as an:
        with trio.fail_after(5):
            assert await to_actor.run(
                add_one,
                an=an,
                n=20,
            ) == 21
        assert not an._children


def test_late_child_reap_registration_is_released():
    '''
    Preserve a nursery-wide reap request across child startup.

    A child monitor can checkpoint while connecting to its parent as
    the surrounding `ActorNursery` begins teardown. Previously the
    nursery signalled only already-registered child events, so a
    monitor registering afterward waited forever. This models that
    ordering by publishing the nursery-wide request first and proves
    the later per-child event inherits its set state immediately.

    '''
    an = object.__new__(tractor.ActorNursery)
    an._join_procs = trio.Event()
    an._child_reap_requests = {}
    an._child_reaped = {}

    an._join_procs.set()
    reap_request, _ = an._register_child_reap(
        ('late_child', 'uid'),
    )

    assert reap_request.is_set()


@tractor_test
async def test_remote_error_from_caller_nursery(
    start_method: str,
    debug_mode: bool,
):
    '''
    With a caller-managed `an` the remote error also
    surfaces in the caller's task, INSIDE the nursery
    block, allowing inline (supervision-style) handling.

    '''
    async with tractor.open_nursery() as an:
        with pytest.raises(RemoteActorError) as excinfo:
            await to_actor.run(
                raise_value_error,
                an=an,
            )

        assert excinfo.value.boxed_type is ValueError


@tractor_test
async def test_reuse_existing_actor_via_portal(
    start_method: str,
    debug_mode: bool,
):
    '''
    Pass `portal=` to schedule the one-shot task in an
    already-running actor; no spawn, no implicit reap.

    '''
    async with tractor.open_nursery() as an:
        portal: tractor.Portal = await an.start_actor(
            'one_shot_worker',
            enable_modules=[__name__],
        )
        for i in range(3):
            assert await to_actor.run(
                add_one,
                portal=portal,
                n=i,
            ) == i + 1

        # still alive: caller owns the actor's lifetime.
        await portal.cancel_actor()


@tractor_test
async def test_concurrent_one_shots_from_task_nursery(
    start_method: str,
    debug_mode: bool,
):
    '''
    The worker-pool-ish pattern from #477: concurrency
    is composed with a plain (caller-side) `trio` task
    nursery scheduling multiple one-shot calls against
    a shared caller-managed actor-nursery; error
    collection thus lives entirely in caller-code.

    '''
    results: dict[int, int] = {}

    async def one_shot(
        an: tractor.ActorNursery,
        i: int,
    ) -> None:
        results[i] = await to_actor.run(
            add_one,
            an=an,
            name=f'one_shot_{i}',
            n=i,
        )

    async with (
        tractor.open_nursery() as an,
        trio.open_nursery() as tn,
    ):
        for i in range(4):
            tn.start_soon(one_shot, an, i)

    assert results == {
        i: i + 1 for i in range(4)
    }


def test_rejects_sync_fn():
    '''
    Non-async callables error BEFORE any spawn (or even
    runtime-boot) happens.

    '''
    def not_async() -> None:
        ...

    with pytest.raises(TypeError):
        trio.run(
            partial(
                to_actor.run,
                not_async,
            )
        )


def test_rejects_streaming_fn():
    '''
    Async-gen (streaming) fns are not one-shot-able,
    same constraint as `Portal.run()`.

    '''
    async def agen():
        yield 1

    with pytest.raises(TypeError):
        trio.run(
            partial(
                to_actor.run,
                agen,
            )
        )


def test_rejects_portal_and_an_combo():
    '''
    `portal=` and `an=` are mutually exclusive
    placement options.

    '''
    with pytest.raises(ValueError):
        trio.run(
            partial(
                to_actor.run,
                add_one,
                portal=object(),
                an=object(),
                n=1,
            )
        )


def test_rejects_runtime_kwargs_with_placement():
    '''
    `runtime_kwargs` only applies when the call opens
    its own private actor-nursery; passing it alongside
    a placement opt is an error, never silently
    ignored.

    '''
    with pytest.raises(ValueError):
        trio.run(
            partial(
                to_actor.run,
                add_one,
                an=object(),
                runtime_kwargs=dict(
                    loglevel='cancel',
                ),
                n=1,
            )
        )
