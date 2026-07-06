"""
Cancellation and error propagation

"""
from functools import partial
import os
import signal
import platform
import time
from itertools import repeat
from typing  import Type

import pytest
import trio
import tractor
from tractor._testing import (
    tractor_test,
)
from tractor._testing.trace import FailAfterWTraceFactory
from tractor.trionics import (
    collapse_eg,
    gather_contexts,
)
from .conftest import no_windows


_non_linux: bool = platform.system() != 'Linux'
_friggin_windows: bool = platform.system() == 'Windows'


pytestmark = [
    # Multi-actor cancel cascades under
    # `--spawn-backend=subint` trip the abandoned-subint
    # GIL-hostage class — a stuck subint can starve the
    # parent's trio loop and block cancel-delivery.
    # Apply the skip module-wide rather than per-test
    # since every test here exercises the same cascade.
    pytest.mark.skipon_spawn_backend(
        'subint',
        reason=(
            'XXX SUBINT GIL-CONTENTION HANGING TEST XXX\n'
            'Cancel cascades under '
            '`--spawn-backend=subint` trip the abandoned-subint '
            'GIL-hostage class — see\n'
            '  - `ai/conc-anal/subint_sigint_starvation_issue.md` '
            '(GIL-hostage, SIGINT-unresponsive)\n'
            '  - `ai/conc-anal/subint_cancel_delivery_hang_issue.md` '
            '(sibling: parent parks on dead chan)\n'
            '  - https://github.com/goodboy/tractor/issues/379 '
            '(subint umbrella)\n'
        )
    ),
    pytest.mark.usefixtures(
        'reap_subactors_per_test',
        # NOTE, cancellation tests stress the SIGKILL
        # `hard_kill` path which leaks UDS sock-files when
        # the subactor's IPC server `finally:` cleanup
        # doesn't run. Track per-test for blame attribution.
        'track_orphaned_uds_per_test',
        # NOTE, cancel-cascade timing races (see
        # `test_nested_multierrors`) can also leave a
        # subactor spinning at 100% CPU when its cancel
        # signal got swallowed mid-handshake. Catches the
        # runaway-loop class that doesn't leak UDS socks
        # but burns the box.
        'detect_runaway_subactors_per_test',
    ),
]


async def assert_err(delay=0):
    await trio.sleep(delay)
    assert 0


@tractor.context
async def assert_err_ctx(
    ctx: tractor.Context,
    delay: float = 0,
) -> None:
    '''
    `@context` shim around `assert_err()` so the multi-actor error
    tests can fan-out one-shot erroring subactors via
    `Portal.open_context()` + `gather_contexts()` instead of the
    removed `ActorNursery.run_in_actor()` (#477).

    '''
    await ctx.started()
    await trio.sleep(delay)
    assert 0


async def sleep_forever():
    await trio.sleep_forever()


async def do_nuthin():
    # just nick the scheduler
    await trio.sleep(0)


@pytest.mark.parametrize(
    'args_err',
    [
        # expected to be thrown in assert_err
        ({}, AssertionError),
        # argument mismatch raised in _invoke()
        ({'unexpected': 10}, TypeError)
    ],
    ids=['no_args', 'unexpected_args'],
)
def test_remote_error(
    reg_addr: tuple,
    args_err: tuple[dict, Type[Exception]],
    set_fork_aware_capture,
):
    '''
    Verify an error raised in a subactor that is propagated
    to the parent nursery, contains the underlying boxed builtin
    error type info and causes cancellation and reraising all the
    way up the stack.

    '''
    args, errtype = args_err

    async def main():
        async with tractor.open_nursery(
            registry_addrs=[reg_addr],
        ) as nursery:

            # `to_actor.run()` blocks on the one-shot's result and
            # raises the remote error directly here in the caller's
            # task (a bad-arg `TypeError` likewise relays as a
            # `RemoteActorError`).
            try:
                await tractor.to_actor.run(
                    assert_err,
                    an=nursery,
                    name='errorer',
                    **args
                )
            except tractor.RemoteActorError as err:
                assert err.boxed_type == errtype
                print("Look Maa that actor failed hard, hehh")
                raise

    # ensure boxed errors
    if args:
        with pytest.raises(tractor.RemoteActorError) as excinfo:
            trio.run(main)

        assert excinfo.value.boxed_type == errtype

    else:
        # the root task will also error on the `Portal.result()`
        # call so we expect an error from there AND the child.
        # |_ tho seems like on new `trio` this doesn't always
        #    happen?
        with pytest.raises((
            BaseExceptionGroup,
            tractor.RemoteActorError,
        )) as excinfo:
            trio.run(main)

        # ensure boxed errors are `errtype`
        err: BaseException = excinfo.value
        if isinstance(err, BaseExceptionGroup):
            suberrs: list[BaseException] = err.exceptions
        else:
            suberrs: list[BaseException] = [err]

        for exc in suberrs:
            assert exc.boxed_type == errtype


def test_multierror(
    reg_addr: tuple[str, int],
    start_method: str,  # parametrized
    set_fork_aware_capture, #: Callable,
):
    '''
    Verify concurrent one-shot subactors erroring propagate a remote
    error out of the `gather_contexts()` fan-out — grouped as a
    `BaseExceptionGroup`, or (under cancel-on-first, where the 2nd
    errorer is cancelled before relaying its own exc) collapsed to a
    single `RemoteActorError`.

    NB the legacy `run_in_actor()` reaped *all* children at nursery
    teardown so this always yielded a BEG-of-N; the `to_actor`
    fan-out is cancel-on-first, so accept either shape.

    '''
    async def main():
        async with tractor.open_nursery(
            registry_addrs=[reg_addr],
        ) as an:

            portals = [
                await an.start_actor(
                    f'errorer{i}',
                    enable_modules=[__name__],
                )
                for i in range(2)
            ]

            # both one-shot subactors error concurrently, so the
            # `gather_contexts()` task-nursery collects them into a
            # `BaseExceptionGroup` (was two non-blocking
            # `run_in_actor()`s reaped at nursery teardown).
            async with gather_contexts(
                mngrs=[
                    p.open_context(assert_err_ctx)
                    for p in portals
                ],
            ):
                pass

    with pytest.raises((
        BaseExceptionGroup,
        tractor.RemoteActorError,
    )):
        trio.run(main)


async def do_nothing():
    pass


@pytest.mark.parametrize(
    'mechanism', [
    'nursery_cancel',
    KeyboardInterrupt,
])
def test_cancel_single_subactor(
    reg_addr: tuple,
    mechanism: str|KeyboardInterrupt,
):
    '''
    Ensure a ``ActorNursery.start_actor()`` spawned subactor
    cancels when the nursery is cancelled.

    '''
    async def spawn_actor():
        '''
        Spawn an actor that blocks indefinitely then cancel via
        either `ActorNursery.cancel()` or an exception raise.

        '''
        async with tractor.open_nursery(
            registry_addrs=[reg_addr],
        ) as nursery:

            portal = await nursery.start_actor(
                'nothin', enable_modules=[__name__],
            )
            assert (await portal.run(do_nothing)) is None

            if mechanism == 'nursery_cancel':
                # would hang otherwise
                await nursery.cancel()
            else:
                raise mechanism

    if mechanism == 'nursery_cancel':
        trio.run(spawn_actor)
    else:
        with pytest.raises(mechanism):
            trio.run(spawn_actor)


async def stream_forever():
    for i in repeat("I can see these little future bubble things"):
        # each yielded value is sent over the ``Channel`` to the
        # parent actor
        yield i
        await trio.sleep(0.01)


@tractor_test(
    timeout=6,
)
async def test_cancel_infinite_streamer(
    reg_addr: tuple,
    start_method: str,
    set_fork_aware_capture,
):
    # stream for at most 1 seconds
    with (
        trio.fail_after(4),
        trio.move_on_after(1) as cancel_scope
    ):
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                'donny',
                enable_modules=[__name__],
            )

            # this async for loop streams values from the above
            # async generator running in a separate process
            async with portal.open_stream_from(stream_forever) as stream:
                async for letter in stream:
                    print(letter)

    # we support trio's cancellation system
    assert cancel_scope.cancelled_caught
    assert n.cancel_called


@pytest.mark.parametrize(
    'num_actors_and_errs',
    [
        # daemon actors sit idle while one-shot task actors error out
        (1, tractor.RemoteActorError, AssertionError, (assert_err, {}), None),
        (2, BaseExceptionGroup, AssertionError, (assert_err, {}), None),
        (3, BaseExceptionGroup, AssertionError, (assert_err, {}), None),

        # 1 daemon actor errors out while one-shot task actors sleep forever
        (3, tractor.RemoteActorError, AssertionError, (sleep_forever, {}),
         (assert_err, {}, True)),
        # daemon actors error out after brief delay while one-shot task
        # actors complete quickly
        (3, tractor.RemoteActorError, AssertionError,
         (do_nuthin, {}), (assert_err, {'delay': 1}, True)),
        # daemon complete quickly delay while one-shot task
        # actors error after brief delay
        (3, BaseExceptionGroup, AssertionError,
         (assert_err, {'delay': 1}), (do_nuthin, {}, False)),
    ],
    ids=[
        '1_one_shot_fails',
        '2_one_shots_fail',
        '3_one_shots_fail',
        '1_daemon_actors_fail',
        '1_daemon_actors_fail_all_one_shots_dun_quick',
        'no_daemon_actors_fail_all_one_shots_sleep_then_fail',
    ],
)
@tractor_test(
    timeout=10,
)
async def test_some_cancels_all(
    num_actors_and_errs: tuple,
    reg_addr: tuple,
    start_method: str,
    loglevel: str,
    set_fork_aware_capture, #: Callable,
):
    '''
    Verify a subset of failed subactors causes all others in
    the nursery to be cancelled just like the strategy in trio.

    This is the first and only supervisory strategy at the moment.

    One-shot subactors run as concurrent `to_actor.run()` tasks
    in a local task-nursery so their errors raise WHILE the
    actor-nursery block is still open (vs the legacy
    `run_in_actor()` teardown-reap); the first error cancels the
    sibling one-shots (whose `trio.Cancelled`s the task-nursery
    absorbs) so the group shape is 1..num_actors
    `RemoteActorError`s depending on relay-vs-cancel timing —
    with `collapse_eg()` unwrapping the deterministic
    single-error cases to a bare `RemoteActorError`.

    '''
    (
        num_actors,
        first_err,
        err_type,
        one_shot_func,
        da_func,
    ) = num_actors_and_errs
    try:
        async with tractor.open_nursery() as an:

            # spawn the same number of deamon actors which should be cancelled
            dactor_portals = []
            for i in range(num_actors):
                dactor_portals.append(await an.start_actor(
                    f'deamon_{i}',
                    enable_modules=[__name__],
                ))

            func, kwargs = one_shot_func
            async with (
                collapse_eg(),
                trio.open_nursery() as tn,
            ):
                for i in range(num_actors):
                    # schedule one-shot task actor(s); errors
                    # raise into this task-nursery scope.
                    tn.start_soon(
                        partial(
                            tractor.to_actor.run,
                            func,
                            an=an,
                            name=f'actor_{i}',
                            **kwargs,
                        )
                    )

                if da_func:
                    func, kwargs, expect_error = da_func
                    for portal in dactor_portals:
                        # if this function fails then we should error
                        # here and the nursery should teardown all
                        # other actors
                        try:
                            await portal.run(func, **kwargs)

                        except tractor.RemoteActorError as err:
                            assert err.boxed_type == err_type
                            # we only expect this first error to propogate
                            # (all other daemons are cancelled before they
                            # can be scheduled)
                            num_actors = 1
                            # reraise so nursery teardown is triggered
                            raise
                        else:
                            if expect_error:
                                pytest.fail(
                                    "Deamon call should fail at checkpoint?")

        # should error here with a `RemoteActorError` or a beg of them

    except (
        BaseExceptionGroup,
        tractor.RemoteActorError,
    ) as _err:
        err = _err
        if isinstance(err, BaseExceptionGroup):
            # only the concurrent multi-error cases can group; the
            # relay-vs-cancel race means anywhere from 1 (all
            # siblings cancelled before relaying) up to all
            # `num_actors` errors may populate the group.
            assert first_err is BaseExceptionGroup
            assert 1 <= len(err.exceptions) <= num_actors
            for exc in err.exceptions:
                assert isinstance(exc, tractor.RemoteActorError)
                assert exc.boxed_type == err_type
        else:
            assert err.boxed_type == err_type

        assert an.cancel_called is True
        assert not an._children
    else:
        pytest.fail("Should have gotten a remote assertion error?")


async def spawn_and_error(
    breadth: int,
    depth: int,
) -> None:
    '''
    Recursively spawn a breadth-wide level of erroring one-shot
    subactors as concurrent `to_actor.run()` tasks; the leaf level
    errors ~simultaneously and each level's task-nursery groups
    whatever `RemoteActorError`s relay before the first one's
    cancel wins, boxing the (`ExceptionGroup`-shaped) group into
    this actor's own relayed error.

    '''
    name = tractor.current_actor().name
    async with (
        tractor.open_nursery() as an,
        trio.open_nursery() as tn,
    ):
        for i in range(breadth):

            if depth > 0:

                args = (
                    spawn_and_error,
                )
                kwargs = {
                    'name': f'spawner_{i}_depth_{depth}',
                    'breadth': breadth,
                    'depth': depth - 1,
                }
            else:
                args = (
                    assert_err,
                )
                kwargs = {
                    'name': f'{name}_errorer_{i}',
                }
            tn.start_soon(
                partial(
                    tractor.to_actor.run,
                    *args,
                    an=an,
                    **kwargs,
                )
            )


# NOTE: `main_thread_forkserver` capture-fd hang class is no
# longer skipped here — `--capture=sys` (the new `pyproject.toml`
# default) sidesteps the pipe-buffer-fill deadlock for
# `test_nested_multierrors`. See
# `ai/conc-anal/subint_forkserver_test_cancellation_leak_issue.md`
# / #449 for the post-mortem.
# @pytest.mark.timeout(
#     10,
#     method='thread',
# )
@pytest.mark.parametrize(
    'depth',
    [1, 3],
    ids='depth={}'.format,
)
@tractor_test(
    # XXX this OUTER `trio.fail_after` wall MUST exceed the
    # largest INNER `fail_after_w_trace()` budget set in the body
    # below (max = the MTF depth=3 == 30s case, further scaled by
    # `cpu_scaling_factor()` on CI/throttle). Otherwise it fires
    # FIRST and pre-empts the inner snapshot-capturing deadline,
    # turning a graceful `TooSlowError`+ptree-dump into an opaque
    # outer timeout-kill (the prior `timeout=10` did exactly this
    # — it was *smaller* than the 12s trio depth=3 budget, so the
    # depth-3 case `FAILED` on slow CI instead of dumping).
    # Trio backend is fast and won't notice the extra budget.
    # See `ai/conc-anal/cancel_cascade_too_slow_under_main_thread_forkserver_issue.md`.
    timeout=40,
)
async def test_nested_multierrors(
    reg_addr: tuple,
    loglevel: str,
    start_method: str,
    set_fork_aware_capture,
    fail_after_w_trace: FailAfterWTraceFactory,
    request: pytest.FixtureRequest,
    depth: int,
):
    '''
    Test that a nested tree of concurrently failing one-shot
    subactors tears down cleanly, relaying (whatever subset of)
    the leaf `AssertionError`s (that win the per-level
    relay-vs-cancel race) re-boxed/grouped at each actor
    boundary.

    Parametrized over recursion `depth ∈ {1, 3}`:

      - `depth=1`: shallow tree (2 spawners × 2 errorers, 2
        levels). Cascade completes well within budget on ALL
        backends including MTF — regression-safety green case.

      - `depth=3`: deep tree (2 spawners × recursive depth-3
        spawn-and-error). On `main_thread_forkserver` this
        trips the cancel-cascade shape-mismatch bug class
        (see `ai/conc-anal/cancel_cascade_too_slow_under_main_thread_forkserver_issue.md`)
        — xfailed below.

    '''
    # XXX: `multiprocessing.forkserver` can't handle nested
    # spawning at any depth — hangs / broken-pipes. Pre-existing
    # backend limitation, NOT depth-specific.
    if start_method == 'forkserver':
        pytest.skip("Forksever sux hard at nested spawning...")

    subactor_breadth = 2

    # MTF backend trips a probabilistic timing race in the
    # cancel-cascade — NOT depth-gated; depth amplifies the
    # variance so depth=3 misses nearly every run while
    # depth=1 misses occasionally. Both get the xfail mark
    # (with `strict=False`) since the bug class can fire at
    # either depth.
    #
    # The scenario in detail:
    #
    #     T=0      spawn spawner_0 + spawner_1 in parallel
    #     T=t1     spawner_0's child errors →
    #              RemoteActorError reaches root nursery
    #     T=t1+ε   root nursery starts cancelling
    #              spawner_1's portal-wait
    #     T=t2     spawner_1's child errors → tries to send
    #              RemoteActorError back
    #
    #     if t2 < t1+ε:  BEG = [RAE, RAE]        ← clean (xpass)
    #     if t2 > t1+ε:  BEG = [RAE, Cancelled]  ← race tripped (xfail)
    #
    # i.e. the assertion below (`isinstance(_, RemoteActorError)`)
    # fails iff cancel-delivery beats the other tree's natural
    # error-propagation. Depth amplifies `t2-t1` variance
    # (longer per-tree paths = more skew); under MTF the
    # fork-spawn jitter + UDS-contention widens both `t1` and
    # `t2` further.
    #
    # NB post-#477 (`to_actor.run()` fan-out in a local
    # task-nursery) a race-tripped sibling's `Cancelled` is
    # ABSORBED by the task-nursery instead of landing in the
    # group — the raced case now shows as a *smaller* BEG, so
    # this marker should consistently `xpass`; drop it once CI
    # confirms.
    #
    # With `strict=False` the clean-cascade cases (most
    # depth=1 runs, rare depth=3 runs) report as `xpassed`
    # while the race-tripped cases report as `xfailed` —
    # neither flakes `--lf`. When MTF cancel-cascade
    # eventually speeds up enough to close the race even at
    # depth=3, BOTH variants will reliably `xpass` and
    # pytest will yell — our signal to drop the marker. See
    # `ai/conc-anal/cancel_cascade_too_slow_under_main_thread_forkserver_issue.md`.
    #
    # Probe CPU throttle ONCE up-front (folds in the sustained-load
    # power-cap that static freq reads miss): used BOTH to inflate
    # the deadline budget below AND to xfail depth=3, whose failure
    # mode under throttle is a runtime-internal reap deadline — not
    # a test-budget miss. See `scripts/cpu-perf-check`.
    from .conftest import cpu_perf_headroom
    headroom: float = cpu_perf_headroom()

    if start_method == 'main_thread_forkserver':
        request.node.add_marker(
            pytest.mark.xfail(
                strict=False,
                reason=(
                    f'MTF cancel-cascade shape-mismatch at '
                    f'depth={depth} (Cancelled races '
                    f'RemoteActorError in BEG); see conc-anal/'
                    'cancel_cascade_too_slow_under_main_thread_forkserver_issue.md'
                ),
            )
        )

    # Under CPU throttle (incl. the sustained-load power-cap that
    # static freq reads miss) the DEEP depth=3 tree trips tractor's
    # INTERNAL reap deadlines (`soft_kill`/`hard_kill`
    # `move_on_after`/`terminate_after=1.6`) before slow subprocs
    # exit, injecting a `Cancelled(source='deadline')` into the BEG
    # — the SAME shape-mismatch class as the MTF xfail above, and
    # NOT fixable by inflating the test-level budget (the Cancelled
    # is minted inside the runtime, not by our `fail_after`).
    # xfail(strict=False) so it auto-clears the moment the box is
    # un-throttled (`headroom == 1.`); depth=1's shallow tree stays
    # under those internal deadlines so it just rides the budget
    # inflation below. See `scripts/cpu-perf-check`.
    elif (
        depth == 3
        and
        headroom != 1.
    ):
        request.node.add_marker(
            pytest.mark.xfail(
                strict=False,
                reason=(
                    'CPU throttled — tractor reap deadline injects '
                    'Cancelled into BEG; see conc-anal/'
                    'trio_033_cancel_cascade_slowdown_depth3_issue.md'
                ),
            )
        )

    # Per-backend/-depth budgets: in the non-hang case the
    # whole spawn + cancel-cascade should complete in well
    # under these. On the borderline hang case the
    # `fail_after_w_trace` fires `TooSlowError` AND captures a
    # ptree/wchan/py-spy snapshot to
    # `$XDG_CACHE_HOME/tractor/hung-dumps/` for offline
    # inspection. See
    # `ai/conc-anal/cancel_cascade_too_slow_under_main_thread_forkserver_issue.md`.
    #
    # NOTE: the `trio` depth=3 budget was bumped 6 -> 12s after
    # the `trio` 0.29 -> 0.33 lock bump (commit c7741bba) slowed
    # the depth-3 cancel-cascade from <6s to ~7-8s; the 6s
    # deadline was firing and its `Cancelled(source='deadline')`
    # (trio 0.33 cancel-reason metadata) collapsed a BEG branch,
    # breaking the `RemoteActorError` assertion below. depth=1
    # still finishes in ~3s so keeps the 6s budget. See
    # `ai/conc-anal/trio_033_cancel_cascade_slowdown_depth3_issue.md`.
    match (start_method, depth):
        case ('trio', 1):
            timeout = 6
        case ('trio', 3):
            timeout = 12
        case ('main_thread_forkserver', 1):
            timeout = 16
        case ('main_thread_forkserver', 3):
            timeout = 30
        # any other fork-based backend (`mp_spawn` et al) pays
        # the same per-spawn round-trip costs as MTF so rides
        # its budgets; without a default arm `timeout` is left
        # unbound -> `UnboundLocalError` at the scaling below.
        case (_, 1):
            timeout = 16
        case (_, 3):
            timeout = 30

    # inflate the budget by the throttle headroom probed above so
    # a slow box doesn't masquerade as a deadline regression.
    # NOTE, `headroom = cpu_perf_headroom()` (set above) is the
    # SUPERSET of `cpu_scaling_factor()` — it folds in the static
    # cpu-freq scaling + slow-CI bump AND the sustained-load
    # throttle probe this depth-3 cascade was the poster child for.
    if headroom != 1.:
        timeout *= headroom

    async with fail_after_w_trace(timeout):
        try:
            async with (
                tractor.open_nursery() as nursery,
                trio.open_nursery() as tn,
            ):
                for i in range(subactor_breadth):
                    tn.start_soon(
                        partial(
                            tractor.to_actor.run,
                            spawn_and_error,
                            an=nursery,
                            name=f'spawner_{i}',
                            breadth=subactor_breadth,
                            depth=depth,
                        )
                    )
        except (
            BaseExceptionGroup,
            tractor.RemoteActorError,
        ) as err:
            # group membership is bounded by the relay-vs-cancel
            # race: the first spawner-tree's error cancels its
            # siblings, whose own errors only group when relayed
            # first; a fully-raced tree even collapses (via the
            # runtime's own `collapse_eg()` unwrapping each level's
            # single-member group) to a bare `RemoteActorError`
            # re-boxing the leaf `AssertionError` at every actor
            # boundary. The deterministic exact-breadth nested-BEG
            # was the legacy `run_in_actor()` reap-all-at-teardown.
            subexcs: list[BaseException] = (
                err.exceptions
                if isinstance(err, BaseExceptionGroup)
                else [err]
            )
            assert 1 <= len(subexcs) <= subactor_breadth
            for subexc in subexcs:
                if (
                    _friggin_windows
                    and
                    isinstance(subexc, trio.Cancelled)
                ):
                    # windows is often too slow and cancellation seems
                    # to happen before an actor is spawned
                    continue

                assert isinstance(subexc, tractor.RemoteActorError)

                accepted: tuple[Type[BaseException], ...] = (
                    # ≥2 sub-tree errors relayed before the
                    # cancel-cascade won → grouped per-level.
                    ExceptionGroup,
                    # every level collapsed down to its lone
                    # relayed (leaf) error.
                    AssertionError,
                    # a mid-level spawner relays an
                    # already-boxed (collapsed) leaf chain,
                    # re-boxing the `RemoteActorError` itself.
                    tractor.RemoteActorError,
                    # under heavy load a runtime-internal reap
                    # deadline can inject a `trio.Cancelled`
                    # into a child's group before relay (the
                    # same class the depth=3 throttle-xfail
                    # covers) upgrading it from an
                    # `ExceptionGroup`.
                    BaseExceptionGroup,
                )
                if _friggin_windows:
                    # on windows it seems we can't exactly be
                    # sure wtf will happen..
                    accepted += (
                        trio.Cancelled,
                    )
                assert subexc.boxed_type in accepted
        else:
            pytest.fail(
                'Should have raised a (grouped) `RemoteActorError`?'
            )


@no_windows
def test_cancel_via_SIGINT(
    reg_addr: tuple,
    loglevel: str,
    start_method: str,
):
    '''
    Ensure that a control-C (SIGINT) signal cancels both the parent and
    child processes in trionic fashion

    '''
    pid: int = os.getpid()

    async def main():
        with trio.fail_after(2):
            async with tractor.open_nursery(
                registry_addrs=[reg_addr],
            ) as tn:
                await tn.start_actor('sucka')
                if 'mp' in start_method:
                    time.sleep(0.1)
                os.kill(pid, signal.SIGINT)
                await trio.sleep_forever()

    with pytest.raises(KeyboardInterrupt):
        trio.run(main)


@no_windows
def test_cancel_via_SIGINT_other_task(
    reg_addr: tuple,
    loglevel: str,
    start_method: str,
    spawn_backend: str,
):
    '''
    Ensure that a control-C (SIGINT) signal cancels both the parent
    and child processes in trionic fashion even a subprocess is
    started from a seperate ``trio`` child  task.

    '''
    from .conftest import cpu_perf_headroom

    pid: int = os.getpid()
    timeout: float = (
        4 if _non_linux
        else 2
    )
    if _friggin_windows:  # smh
        timeout += 1

    # latency headroom for static cpu-freq scaling + sustained-load
    # throttle + CI (auto-cpufreq et al.); see `cpu_perf_headroom()`.
    headroom: float = cpu_perf_headroom()
    if headroom != 1.:
        timeout *= headroom

    async def spawn_and_sleep_forever(
        task_status=trio.TASK_STATUS_IGNORED
    ):
        async with tractor.open_nursery(
            registry_addrs=[reg_addr],
        ) as an:
            # just keep a set of (daemon) subactors alive for the
            # SIGINT to cancel (was 3 `run_in_actor(sleep_forever)`
            # one-shots — a daemon needs no "main" task to idle).
            for i in range(3):
                await an.start_actor(
                    f'namesucka_{i}',
                )
            task_status.started()
            await trio.sleep_forever()

    async def main():
        # should never timeout since SIGINT should cancel the current program
        with trio.fail_after(timeout):
            async with trio.open_nursery() as tn:
                await tn.start(spawn_and_sleep_forever)
                if 'mp' in spawn_backend:
                    time.sleep(0.1)
                os.kill(pid, signal.SIGINT)

    # SIGINT -> `KeyboardInterrupt`; under `trio>=0.25`'s strict
    # exception-groups the KI surfaces wrapped in a (cancel-padded)
    # `BaseExceptionGroup` rather than bare — so accept either form
    # (replaces the now-deprecated `strict_exception_groups=False`,
    # and `collapse_eg()` can't help since the group is multi-exc:
    # the KI rides alongside the child-task `Cancelled`s).
    with pytest.raises(BaseException) as excinfo:
        trio.run(main)
    exc = excinfo.value
    assert (
        isinstance(exc, KeyboardInterrupt)
        or (
            isinstance(exc, BaseExceptionGroup)
            and exc.subgroup(KeyboardInterrupt) is not None
        )
    )


async def spin_for(period=3):
    "Sync sleep."
    print(f'sync sleeping in sub-sub for {period}\n')
    time.sleep(period)


async def spawn_sub_with_sync_blocking_task():
    async with tractor.open_nursery() as an:
        print('starting sync blocking subactor..\n')
        # one-shot: parks HERE awaiting the sync-sleeping
        # grandchild's result until cancelled from above.
        await tractor.to_actor.run(
            spin_for,
            an=an,
            name='sleeper',
        )
        print('exiting first subactor layer..\n')


@pytest.mark.parametrize(
    'man_cancel_outer',
    [
        False,  # passes if delay != 2

        # always causes an unexpected eg-w-embedded-assert-err?
        pytest.param(True,
             marks=pytest.mark.xfail(
                 reason=(
                    'always causes an unexpected eg-w-embedded-assert-err?'
                )
            ),
        ),
    ],
)
@no_windows
def test_cancel_while_childs_child_in_sync_sleep(
    loglevel: str,
    start_method: str,
    is_forking_spawner: bool,
    debug_mode: bool,
    reg_addr: tuple,
    man_cancel_outer: bool,
):
    '''
    Verify that a child cancelled while executing sync code is torn
    down even when that cancellation is triggered by the parent
    2 nurseries "up".

    Though the grandchild should stay blocking its actor runtime, its
    parent should issue a "zombie reaper" to hard kill it after
    sufficient timeout.

    '''
    if start_method == 'forkserver':
        pytest.skip(
            "`multiprocessing`'s forkserver sux hard at "
            "resuming from sync sleep..."
        )

    async def main():
        #
        # XXX BIG TODO NOTE XXX
        #
        # it seems there's a strange race that can happen
        # where where the fail-after will trigger outer scope
        # .cancel() which then causes the inner scope to raise,
        #
        # BaseExceptionGroup('Exceptions from Trio nursery', [
        #   BaseExceptionGroup('Exceptions from Trio nursery',
        #   [
        #       Cancelled(),
        #       Cancelled(),
        #   ]
        #   ),
        #   AssertionError('assert 0')
        # ])
        #
        # WHY THIS DOESN'T MAKE SENSE:
        # ---------------------------
        # - it should raise too-slow-error when too slow..
        #  * verified that using simple-cs and manually cancelling
        #    you get same outcome -> indicates that the fail-after
        #    can have its TooSlowError overriden!
        #  |_ to check this it's easy, simplly decrease the timeout
        #     as per the var below.
        #
        # - when using the manual simple-cs the outcome is different
        #   DESPITE the `assert 0` which means regardless of the
        #   inner scope effectively failing in the same way, the
        #   bubbling up **is NOT the same**.
        #
        # delays trigger diff outcomes..
        # ---------------------------
        # as seen by uncommenting various lines below there is from
        # my POV an unexpected outcome due to the delay=2 case.
        #
        # delay = 1  # no AssertionError in eg, TooSlowError raised.
        # delay = 2  # is AssertionError in eg AND no TooSlowError !?
        # is AssertionError in eg AND no _cs cancellation.
        delay = (
            6 if (
                _non_linux
                or
                is_forking_spawner
            )
            else 4 
        )

        with trio.fail_after(delay) as _cs:
        # with trio.CancelScope() as cs:
        # ^XXX^ can be used instead to see same outcome.

            async with (
                # tractor.trionics.collapse_eg(),  # doesn't help
                tractor.open_nursery(
                    hide_tb=False,
                    debug_mode=debug_mode,
                    registry_addrs=[reg_addr],
                ) as an,
                trio.open_nursery() as tn,
            ):
                # bg one-shot: parks on the middle actor's result
                # (itself parked on the sync-sleeping grandchild)
                # until the `assert 0` below cancels this scope.
                tn.start_soon(
                    partial(
                        tractor.to_actor.run,
                        spawn_sub_with_sync_blocking_task,
                        an=an,
                        name='sync_blocking_sub',
                    )
                )
                await trio.sleep(1)

                if man_cancel_outer:
                    print('Cancelling manually in root')
                    _cs.cancel()

                # trigger exc-srced taskc down
                # the actor tree.
                print('RAISING IN ROOT')
                assert 0

    with pytest.raises(AssertionError):
        trio.run(main)


def test_fast_graceful_cancel_when_spawn_task_in_soft_proc_wait_for_daemon(
    start_method: str,
):
    '''
    This is a very subtle test which demonstrates how cancellation
    during process collection can result in non-optimal teardown
    performance on daemon actors. The fix for this test was to handle
    ``trio.Cancelled`` specially in the spawn task waiting in
    `proc.wait()` such that ``Portal.cancel_actor()`` is called before
    executing the "hard reap" sequence (which has an up to 3 second
    delay currently).

    In other words, if we can cancel the actor using a graceful remote
    cancellation, and it's faster, we might as well do it.

    '''
    kbi_delay = 0.5
    timeout: float = 2.9

    if _friggin_windows:  # smh
        timeout += 1

    # CPU-scaling / sustained-throttle / CI latency headroom — macOS
    # CI especially is slow for this graceful-vs-hard-reap timing
    # race; see `cpu_perf_headroom()`.
    from .conftest import cpu_perf_headroom
    timeout *= cpu_perf_headroom()

    async def main():
        start = time.time()
        try:
            async with trio.open_nursery() as nurse:
                async with tractor.open_nursery() as tn:
                    p = await tn.start_actor(
                        'fast_boi',
                        enable_modules=[__name__],
                    )

                    async def delayed_kbi():
                        await trio.sleep(kbi_delay)
                        print(f'RAISING KBI after {kbi_delay} s')
                        raise KeyboardInterrupt

                    # start task which raises a kbi **after**
                    # the actor nursery ``__aexit__()`` has
                    # been run.
                    nurse.start_soon(delayed_kbi)

                    await p.run(do_nuthin)

        # need to explicitly re-raise the lone kbi..now
        except* KeyboardInterrupt as kbi_eg:
            assert (len(excs := kbi_eg.exceptions) == 1)
            raise excs[0]

        finally:
            duration = time.time() - start
            if duration > timeout:
                raise trio.TooSlowError(
                    'daemon cancel was slower then necessary..'
                )

    with pytest.raises(KeyboardInterrupt):
        trio.run(main)
