'''
`tractor.to_actor`: one-shot single-remote-task API suite.

Verifies the "spiritual successor" to (and eventual
replacement of) `ActorNursery.run_in_actor()`; see
https://github.com/goodboy/tractor/issues/477

'''
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
import trio
from trio.testing import MockClock
import tractor
from tractor import (
    RemoteActorError,
    to_actor,
)
from tractor._testing import tractor_test
from tractor._exceptions import ActorTooSlowError
from tractor.msg.ptr import NamespacePath
from tractor.spawn import _mp as mp_spawn
from tractor.to_actor import _api as to_actor_api

from ._helpers import (
    CancellationMarkers,
    non_registration_contexts,
)


async def add_one(
    n: int,
) -> int:
    '''
    Increment within an active actor runtime.

    '''
    assert tractor.current_actor(
        err_on_no_runtime=False,
    ) is not None
    return n + 1


async def raise_value_error() -> None:
    raise ValueError('kaboom')


async def echo_control_names(
    value: int,
    /,
    *,
    name: str,
    portal: str,
    an: str,
    runtime_kwargs: str,
) -> dict[str, int|str]:
    return {
        'value': value,
        'name': name,
        'portal': portal,
        'an': an,
        'runtime_kwargs': runtime_kwargs,
    }


async def mark_task_cancellation(
    started_path: str,
    cancelled_path: str,
) -> None:
    with CancellationMarkers(
        started_path,
        cancelled_path,
    ):
        await trio.sleep_forever()


async def echo_startup_control(
    _cancel_on_startup: str,
) -> str:
    return _cancel_on_startup


async def collect_args(
    *args: object,
) -> tuple[object, ...]:
    return args


async def collect_call(
    *args: object,
    **kwargs: object,
) -> tuple[tuple[object, ...], dict[str, object]]:
    return args, kwargs


def test_public_module_alias() -> None:
    '''
    Keep the public trampoline alias separate from its private module.

    Callers use `to_actor.MODULE` to configure an existing actor's RPC
    allowlist, while `_api.__name__` remains the authoritative module
    path and does not re-export the alias internally.

    '''
    assert to_actor.MODULE == to_actor_api.__name__
    assert not hasattr(to_actor_api, 'MODULE')


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
        1,
    ) == 2


def test_one_shot_boots_implicit_runtime(
    reg_addr: tuple,
    start_method: str,
    loglevel: str,
):
    '''
    Outside any actor-runtime `to_actor.run()` boots one
    implicitly (just like bare `open_nursery()` usage)
    configured via pass-through `runtime_kwargs`. The remote target
    asserts its runtime exists; the caller then verifies the private
    runtime is fully torn down before `to_actor.run()` returns.

    '''
    async def main() -> None:
        assert tractor.current_actor(
            err_on_no_runtime=False,
        ) is None
        result = await to_actor.run(
            add_one,
            41,
            runtime_kwargs=dict(
                registry_addrs=[reg_addr],
                start_method=start_method,
                loglevel=loglevel,
            ),
        )
        assert result == 42
        assert tractor.current_actor(
            err_on_no_runtime=False,
        ) is None

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
    and its `ActorNursery` child/reap bookkeeping remained alive until
    the entire nursery exited. The assertions inside the still-open
    nursery prove child-process joining and removal from all three
    mappings complete before the one-shot call returns.

    '''
    async with tractor.open_nursery() as an:
        assert await to_actor.run(
            add_one,
            10,
            an=an,
        ) == 11
        assert not an._children
        assert not an._child_reap_requests
        assert not an._child_reaped


@tractor_test
async def test_cancel_ack_failure_hard_reaps_child(
    monkeypatch: pytest.MonkeyPatch,
    start_method: str,
    debug_mode: bool,
):
    '''
    Escalate a failed cancel acknowledgement and reap the child.

    `Portal.cancel_actor()` catches `TransportClosed` and returns
    `False` when it can not confirm runtime cancellation. The mock
    represents that public post-transport-failure result, so no
    underlying exception remains to bubble through `to_actor.run()`.

    The old one-shot path ignored `False`, released the nursery-wide
    join gate and then waited forever for a still-running process.
    `_cancel_and_reap_child()` must instead hard-kill and join the child.
    The five-second scope is only a generous CI hang ceiling: normal
    teardown returns much sooner, while expiry fails the test. Final
    assertions prove the child monitor removes every `ActorNursery`
    child/reap entry before returning.

    '''
    async def cancel_without_ack(
        portal: tractor.Portal,
        timeout: float|None = None,
        raise_on_timeout: bool = False,
    ) -> bool:
        assert raise_on_timeout
        return False

    # Model `Portal.cancel_actor()` after it catches `TransportClosed`;
    # there is no transport exception left for `run()` to re-raise.
    monkeypatch.setattr(
        tractor.Portal,
        'cancel_actor',
        cancel_without_ack,
    )

    async with tractor.open_nursery() as an:
        # Expiry means hard reaping hung; five seconds is not the
        # expected duration of the successful path.
        with trio.fail_after(5):
            assert await to_actor.run(
                add_one,
                20,
                an=an,
            ) == 21
        assert not an._children
        assert not an._child_reap_requests
        assert not an._child_reaped


def test_cancel_actor_shares_request_and_ack_deadline():
    '''
    Share one cancel deadline across request publication and ack waiting.

    The cancel RPC's outer timeout cannot penetrate a complete-frame
    shield. The fake private RPC applies the forwarded send deadline to
    its own shielded wait, then checkpoints into the outer scope. A
    bounded `ActorTooSlowError` and the recorded absolute deadline prove
    publication and acknowledgement share one timeout budget.

    A real subactor can not deterministically stall the caller's
    outbound frame at this exact boundary. Actual partial-frame stream
    closure is covered by
    `test_transport_send_deadline_closes_partial_frame()`.

    '''
    class ConnectedChannel:
        def __init__(self) -> None:
            self._cancel_called = False
            self.aid = tractor.msg.Aid(
                name='blocked_peer',
                uuid='test',
            )

        def connected(self) -> bool:
            return True

    async def main() -> None:
        channel = ConnectedChannel()
        # `Portal.__init__()` requires live actor-runtime state; this
        # unit seam needs only its channel and private RPC method.
        portal = object.__new__(tractor.Portal)
        portal._chan = channel
        deadlines: list[float] = []

        async def blocked_cancel(
            namespace: str,
            function: str,
            kwargs: dict[str, object],
            cancel_on_startup: bool,
            send_deadline: float,
        ) -> None:
            assert (namespace, function) == ('self', 'cancel')
            assert kwargs == {}
            assert not cancel_on_startup
            deadlines.append(send_deadline)
            with trio.CancelScope(
                deadline=send_deadline,
                shield=True,
            ):
                await trio.sleep_forever()
            await trio.lowlevel.checkpoint_if_cancelled()

        portal._run_from_ns = blocked_cancel
        with pytest.raises(ActorTooSlowError):
            await portal.cancel_actor(
                timeout=1,
                raise_on_timeout=True,
            )

        assert deadlines == [1.]

    trio.run(
        main,
        clock=MockClock(autojump_threshold=0),
    )


def test_context_cancel_shares_request_and_ack_deadline():
    '''
    Bound context-cancel publication and acknowledgement together.

    `Context.cancel()` shields its transaction from outer cancellation,
    while `MsgpackTransport.send()` separately shields complete frame
    publication. Previously the context's timeout was not forwarded to
    that inner shield, so a peer which stopped reading could leave the
    cancel task blocked forever instead of respecting `timeout`.

    The fake private RPC records the forwarded absolute deadline and
    blocks under a send-like shield until that deadline. The mock clock
    advances directly to it; completion and the exact recorded value
    prove that publication shares the context's one-second budget.
    The transport suite separately proves that deadline expiry closes
    a stream after partial frame publication.

    '''
    async def main() -> None:
        deadlines: list[float] = []

        async def blocked_cancel(
            namespace: str,
            function: str,
            kwargs: dict[str, object],
            cancel_on_startup: bool,
            send_deadline: float,
        ) -> None:
            assert (namespace, function) == ('self', '_cancel_task')
            assert kwargs == {'cid': 'blocked-context'}
            assert not cancel_on_startup
            deadlines.append(send_deadline)
            with trio.CancelScope(
                deadline=send_deadline,
                shield=True,
            ):
                await trio.sleep_forever()
            await trio.lowlevel.checkpoint_if_cancelled()

        peer_aid = tractor.msg.Aid(
            name='blocked_peer',
            uuid='test',
        )

        def connected() -> bool:
            return True

        # `Context.__init__()` requires live actor/channel registration;
        # this unit seam supplies only state consumed by `.cancel()`.
        ctx = object.__new__(tractor.Context)
        ctx.chan = SimpleNamespace(
            aid=peer_aid,
            connected=connected,
            transport=SimpleNamespace(maddr='test://blocked'),
        )
        ctx.cid = 'blocked-context'
        ctx._portal = SimpleNamespace(_run_from_ns=blocked_cancel)
        ctx._nsf = NamespacePath.from_ref(add_one)

        await ctx.cancel(timeout=1)

        assert deadlines == [1.]

    trio.run(
        main,
        clock=MockClock(autojump_threshold=0),
    )


def _mock_actor_nursery() -> tractor.ActorNursery:
    an = object.__new__(tractor.ActorNursery)
    an._children = {}
    an._join_procs = trio.Event()
    an._child_reap_requests = {}
    an._child_reaped = {}
    an._at_least_one_child_in_debug = False
    an._cancel_called = False
    return an


def test_late_child_registration_observes_cancel():
    '''
    Make registration atomically observe nursery cancellation.

    `ActorNursery.cancel()` previously snapshotted `_children` before
    its next checkpoint. A process monitor registering after that
    snapshot received a reap request but no runtime cancellation, then
    waited forever for natural exit. Publishing the child and its reap
    events together returns cancellation ownership to the late monitor.

    '''
    an = _mock_actor_nursery()
    # `ActorNursery.cancel()` has set its sticky flag after taking the
    # old `_children` snapshot but before backend registration resumes.
    an._cancel_called = True
    aid = tractor.msg.Aid(
        name='late_child',
        uuid='test',
    )
    subactor = SimpleNamespace(aid=aid)
    proc = object()

    (
        reap_request,
        reaped,
        cancel_during_registration,
    ) = an._register_child(
        subactor,
        proc,
        None,
    )

    assert cancel_during_registration
    assert an._children[aid.uid] == (
        subactor,
        proc,
        None,
    )
    assert an._child_reap_requests[aid] is reap_request
    assert an._child_reaped[aid] is reaped


def test_mp_late_registration_never_starts_process(
    monkeypatch: pytest.MonkeyPatch,
):
    '''
    Refuse to start an MP child already owned by nursery cancellation.

    A concurrent `ActorNursery.cancel()` can publish cancellation after
    `start_actor()` checks its flag but before the MP backend registers
    its process. The fake registration reports that exact schedule.
    Proving `FakeProcess.start()` is never called prevents a child from
    starting after it was omitted from the cancellation snapshot.

    '''
    class FakeProcess:
        started: bool = False

        def start(self) -> None:
            self.started = True

    process = FakeProcess()

    def register_child(
        subactor: object,
        proc: object,
        portal: object|None,
    ) -> tuple[trio.Event, trio.Event, bool]:
        '''
        Simulate provisional MP registration before child startup.

        '''
        assert subactor
        assert proc is process
        assert portal is None
        return (
            trio.Event(),
            trio.Event(),
            True,
        )

    class FakeContext:
        def get_start_method(self) -> str:
            return 'spawn'

        def Process(self, **kwargs: object) -> FakeProcess:
            assert kwargs
            return process

    nursery = SimpleNamespace(
        _register_child=register_child,
    )
    subactor = SimpleNamespace(
        aid=tractor.msg.Aid(
            name='late_mp_child',
            uuid='test',
        ),
    )
    monkeypatch.setattr(
        mp_spawn._spawn,
        '_ctx',
        FakeContext(),
    )

    with pytest.raises(
        RuntimeError,
        match='nursery began cancelling',
    ):
        trio.run(
            partial(
                mp_spawn.mp_proc,
                name='late_mp_child',
                actor_nursery=nursery,
                subactor=subactor,
                errors={},
                bind_addrs=[],
                parent_addr=SimpleNamespace(),
                _runtime_vars={},
            )
        )

    assert not process.started


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

    # Nursery teardown publishes its reap request while the child
    # monitor is checkpointed before per-child event registration.
    an._join_procs.set()
    aid = tractor.msg.Aid(
        name='late_child',
        uuid='uid',
    )
    reap_request, _ = an._register_child_reap(aid)

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

    The low-level `Portal.run_from_ns()` assertion also proves its
    target kwargs remain separate from the private startup-cancel
    policy used by context cleanup.

    '''
    async with tractor.open_nursery() as an:
        actor = tractor.current_actor()
        portal: tractor.Portal = await an.start_actor(
            'one_shot_worker',
            enable_modules=[
                __name__,
                to_actor.MODULE,
            ],
        )
        contexts_before = non_registration_contexts(actor)
        for i in range(3):
            assert await to_actor.run(
                add_one,
                i,
                portal=portal,
            ) == i + 1

        assert await portal.run_from_ns(
            __name__,
            'echo_startup_control',
            _cancel_on_startup='target_value',
        ) == 'target_value'
        assert non_registration_contexts(actor) == contexts_before

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
            i,
            an=an,
            name=f'one_shot_{i}',
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


def test_partial_placeholder_normalization(
    monkeypatch: pytest.MonkeyPatch,
):
    '''
    Preserve Python 3.14 `functools.partial` placeholder semantics.

    The test environment runs Python 3.13, so this installs an identity
    sentinel matching Python 3.14's `functools.Placeholder` API.
    Interleaved placeholders prove call-time positional arguments are
    merged in order. Undersupply and a mismatched final target
    signature both fail locally before actor runtime startup.

    '''
    placeholder = object()
    monkeypatch.setattr(
        to_actor_api.functools,
        'Placeholder',
        placeholder,
        raising=False,
    )
    fn = partial(
        collect_args,
        placeholder,
        2,
        placeholder,
    )
    normalized_fn, args, kwargs = to_actor_api._normalize_call(
        fn,
        (1, 3, 4),
    )
    assert normalized_fn is collect_args
    assert args == (1, 2, 3, 4)
    assert kwargs == {}

    with pytest.raises(TypeError, match='Not enough positional'):
        to_actor_api._normalize_call(fn, (1,))

    with pytest.raises(TypeError, match='too many positional'):
        to_actor_api._normalize_call(
            partial(add_one, 1),
            (2,),
        )


def test_nested_partial_normalization():
    '''
    Flatten every retained `functools.partial` layer before RPC.

    CPython normally combines nested partials, but preserves the inner
    object when it has instance attributes. Unwrapping only the outer
    layer left a non-namespace-addressable partial as the RPC target.
    The custom attribute triggers that retained shape; the assertions
    prove positional ordering and outer-keyword precedence match a
    direct nested-partial call.

    '''
    inner = partial(
        collect_call,
        1,
        label='inner',
    )
    inner.note = 'retain this partial layer'
    outer = partial(
        inner,
        2,
        label='outer',
    )

    fn, args, kwargs = to_actor_api._normalize_call(
        outer,
        (3,),
    )
    assert fn is collect_call
    assert args == (1, 2, 3)
    assert kwargs == {'label': 'outer'}


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
                1,
                portal=object(),
                an=object(),
            )
        )


@pytest.mark.parametrize(
    'placement',
    ['an', 'portal'],
)
@pytest.mark.parametrize(
    'runtime_kwargs',
    [
        {},
        {'loglevel': 'cancel'},
    ],
    ids=['empty', 'configured'],
)
def test_rejects_runtime_kwargs_with_placement(
    placement: str,
    runtime_kwargs: dict,
):
    '''
    `runtime_kwargs` only applies when the call opens
    its own private actor-nursery; passing it alongside
    a placement opt is an error, never silently
    ignored. In particular, an empty dict still means the
    caller provided this mutually exclusive option; testing
    both placement modes prevents truthiness checks from
    accepting it before any actor runtime is started.

    '''
    with pytest.raises(ValueError):
        trio.run(
            partial(
                to_actor.run,
                add_one,
                1,
                **{
                    placement: object(),
                    'runtime_kwargs': runtime_kwargs,
                },
            )
        )


@tractor_test
async def test_trio_style_args_and_partial_kwargs(
    start_method: str,
    debug_mode: bool,
):
    '''
    Forward positional args and partial-bound keyword arguments.

    The original API captured every keyword matching an actor
    control, so ordinary target parameters such as `name`, `portal`,
    `an` and `runtime_kwargs` could not be called. This test uses a
    positional-only target argument plus all colliding keyword names.
    Binding the target keywords with `functools.partial()` proves the
    Trio-style calling convention keeps target inputs separate from
    actor controls.

    '''
    fn = partial(
        echo_control_names,
        name='target_name',
        portal='target_portal',
        an='target_an',
        runtime_kwargs='target_runtime_kwargs',
    )
    async with tractor.open_nursery() as an:
        result = await to_actor.run(
            fn,
            42,
            an=an,
            name='actor_name',
        )

    assert result == {
        'value': 42,
        'name': 'target_name',
        'portal': 'target_portal',
        'an': 'target_an',
        'runtime_kwargs': 'target_runtime_kwargs',
    }


@tractor_test
async def test_portal_task_cancelled_with_local_caller(
    tmp_path: Path,
    start_method: str,
    debug_mode: bool,
):
    '''
    Couple a reused portal's remote task to its local caller.

    The former `Portal.run()` path abandoned its remote task when the
    local `to_actor.run()` caller was cancelled. The target writes
    one file after starting and another from its cancellation
    `finally`. Cancelling the local task nursery and observing the
    second file proves `Portal.open_context()` propagated
    cancellation before the caller exited. A subsequent call proves
    the caller-owned actor was not cancelled with that task.

    '''
    started_path = tmp_path / 'started'
    cancelled_path = tmp_path / 'cancelled'

    async with tractor.open_nursery() as an:
        actor = tractor.current_actor()
        portal: tractor.Portal = await an.start_actor(
            'context_worker',
            enable_modules=[
                __name__,
                to_actor.MODULE,
            ],
        )
        contexts_before = non_registration_contexts(actor)

        async with trio.open_nursery() as tn:
            tn.start_soon(
                partial(
                    to_actor.run,
                    mark_task_cancellation,
                    str(started_path),
                    str(cancelled_path),
                    portal=portal,
                ),
            )
            with trio.fail_after(5):
                while not started_path.exists():
                    await trio.sleep(0.01)
            tn.cancel_scope.cancel()

        assert cancelled_path.exists()
        assert non_registration_contexts(actor) == contexts_before
        assert await to_actor.run(
            add_one,
            1,
            portal=portal,
        ) == 2
        assert non_registration_contexts(actor) == contexts_before

        await portal.cancel_actor()


@tractor_test
async def test_context_trampoline_preserves_module_allowlist(
    start_method: str,
    debug_mode: bool,
):
    '''
    Keep target resolution behind the actor's RPC module allowlist.

    Loading the target with `NamespacePath.load_ref()` would silently
    bypass the actor's existing module-exposure boundary. This actor
    exposes only the trusted trampoline, not the test module; the
    boxed `ModuleNotExposed` proves the trampoline delegates target
    resolution to `Actor._get_rpc_func()`.

    '''
    async with tractor.open_nursery() as an:
        actor = tractor.current_actor()
        portal: tractor.Portal = await an.start_actor(
            'restricted_context_worker',
            enable_modules=[to_actor.MODULE],
        )
        contexts_before = non_registration_contexts(actor)
        with pytest.raises(RemoteActorError) as excinfo:
            await to_actor.run(
                add_one,
                1,
                portal=portal,
            )

        assert excinfo.value.boxed_type is tractor.ModuleNotExposed
        assert non_registration_contexts(actor) == contexts_before
        await portal.cancel_actor()


@tractor_test
async def test_portal_requires_context_trampoline(
    start_method: str,
    debug_mode: bool,
):
    '''
    Require explicit trampoline exposure on a caller-owned actor.

    Automatically exposing the module in every actor weakens the RPC
    allowlist for actors that never use `to_actor.run()`. A portal to
    such an actor instead fails with the usual `ModuleNotExposed`,
    naming the module callers must opt into.

    '''
    async with tractor.open_nursery() as an:
        actor = tractor.current_actor()
        portal: tractor.Portal = await an.start_actor(
            'no_context_trampoline_worker',
            enable_modules=[__name__],
        )
        contexts_before = non_registration_contexts(actor)
        with pytest.raises(RemoteActorError) as excinfo:
            await to_actor.run(
                add_one,
                1,
                portal=portal,
            )

        err = excinfo.value
        assert err.boxed_type is tractor.ModuleNotExposed
        assert to_actor.MODULE in str(err)
        assert non_registration_contexts(actor) == contexts_before
        await portal.cancel_actor()
