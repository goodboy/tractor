"""
Cancellation and error propagation

"""
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

            # on a remote type error caused by bad input args
            # this should raise directly which means we **don't** get
            # an exception group outside the nursery since the error
            # here and the far end task error are one in the same?
            portal = await nursery.run_in_actor(
                assert_err,
                name='errorer',
                **args
            )

            # get result(s) from main task
            try:
                # this means the root actor will also raise a local
                # parent task error and thus an eg will propagate out
                # of this actor nursery.
                await portal.result()
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
    Verify we raise a ``BaseExceptionGroup`` out of a nursery where
    more then one actor errors.

    '''
    async def main():
        async with tractor.open_nursery(
            registry_addrs=[reg_addr],
        ) as nursery:

            await nursery.run_in_actor(assert_err, name='errorer1')
            portal2 = await nursery.run_in_actor(assert_err, name='errorer2')

            # get result(s) from main task
            try:
                await portal2.result()
            except tractor.RemoteActorError as err:
                assert err.boxed_type is AssertionError
                print("Look Maa that first actor failed hard, hehh")
                raise

        # here we should get a ``BaseExceptionGroup`` containing exceptions
        # from both subactors

    with pytest.raises(BaseExceptionGroup):
        trio.run(main)


@pytest.mark.parametrize(
    'delay',
    (0, 0.5),
    ids='delays={}'.format,
)
@pytest.mark.parametrize(
    'num_subactors',
    range(25, 26),
    ids= 'num_subs={}'.format,
)
def test_multierror_fast_nursery(
    reg_addr: tuple,
    start_method: str,
    num_subactors: int,
    delay: float,
    set_fork_aware_capture,
    fail_after_w_trace: FailAfterWTraceFactory,
):
    '''
    Verify we raise a ``BaseExceptionGroup`` out of a nursery where
    more then one actor errors and also with a delay before failure
    to test failure during an ongoing spawning.

    '''
    async def main():
        # budget = 2× natural trio-backend cascade time for
        # 25 errorer subactors (~14s observed). on-timeout
        # diag snapshot → if the cancel cascade hangs
        # (observed under MTF backend with N>=14 errorer
        # subactors) we get a fresh ptree/wchan/py-spy dump
        # on disk INSTEAD of an opaque pytest timeout-kill.
        # See `tractor/_testing/trace.py` for the helper.
        async with fail_after_w_trace(30.0):
            async with tractor.open_nursery(
                registry_addrs=[reg_addr],
            ) as nursery:

                for i in range(num_subactors):
                    await nursery.run_in_actor(
                        assert_err,
                        name=f'errorer{i}',
                        delay=delay
                    )

    # with pytest.raises(trio.MultiError) as exc_info:
    # NOTE, `trio.TooSlowError` from `fail_after_w_trace`
    # bubbles UN-wrapped if `open_nursery.__aexit__` never
    # gets re-entered; wrapped inside a `BaseExceptionGroup`
    # if it did. Accept both shapes so the matcher itself
    # doesn't lie about *what* failed.
    with pytest.raises(
        (BaseExceptionGroup, trio.TooSlowError),
    ) as exc_info:
        trio.run(main)

    if isinstance(exc_info.value, trio.TooSlowError):
        pytest.fail(
            f'cancel cascade hung past 12s '
            f'(num_subactors={num_subactors}, delay={delay}); '
            f'see stderr for `fail_after_w_trace` snapshot path'
        )

    assert exc_info.type == ExceptionGroup
    err = exc_info.value
    exceptions = err.exceptions

    if len(exceptions) == 2:
        # sometimes oddly now there's an embedded BrokenResourceError ?
        for exc in exceptions:
            excs = getattr(exc, 'exceptions', None)
            if excs:
                exceptions = excs
                break

    assert len(exceptions) == num_subactors

    for exc in exceptions:
        assert isinstance(exc, tractor.RemoteActorError)
        assert exc.boxed_type is AssertionError


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
        # daemon actors sit idle while single task actors error out
        (1, tractor.RemoteActorError, AssertionError, (assert_err, {}), None),
        (2, BaseExceptionGroup, AssertionError, (assert_err, {}), None),
        (3, BaseExceptionGroup, AssertionError, (assert_err, {}), None),

        # 1 daemon actor errors out while single task actors sleep forever
        (3, tractor.RemoteActorError, AssertionError, (sleep_forever, {}),
         (assert_err, {}, True)),
        # daemon actors error out after brief delay while single task
        # actors complete quickly
        (3, tractor.RemoteActorError, AssertionError,
         (do_nuthin, {}), (assert_err, {'delay': 1}, True)),
        # daemon complete quickly delay while single task
        # actors error after brief delay
        (3, BaseExceptionGroup, AssertionError,
         (assert_err, {'delay': 1}), (do_nuthin, {}, False)),
    ],
    ids=[
        '1_run_in_actor_fails',
        '2_run_in_actors_fail',
        '3_run_in_actors_fail',
        '1_daemon_actors_fail',
        '1_daemon_actors_fail_all_run_in_actors_dun_quick',
        'no_daemon_actors_fail_all_run_in_actors_sleep_then_fail',
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

    '''
    (
        num_actors,
        first_err,
        err_type,
        ria_func,
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

            func, kwargs = ria_func
            riactor_portals = []
            for i in range(num_actors):
                # start actor(s) that will fail immediately
                riactor_portals.append(
                    await an.run_in_actor(
                        func,
                        name=f'actor_{i}',
                        **kwargs
                    )
                )

            if da_func:
                func, kwargs, expect_error = da_func
                for portal in dactor_portals:
                    # if this function fails then we should error here
                    # and the nursery should teardown all other actors
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

        # should error here with a ``RemoteActorError`` or ``MultiError``

    except first_err as _err:
        err = _err
        if isinstance(err, BaseExceptionGroup):
            assert len(err.exceptions) == num_actors
            for exc in err.exceptions:
                if isinstance(exc, tractor.RemoteActorError):
                    assert exc.boxed_type == err_type
                else:
                    assert isinstance(exc, trio.Cancelled)
        elif isinstance(err, tractor.RemoteActorError):
            assert err.boxed_type == err_type

        assert an.cancel_called is True
        assert not an._children
    else:
        pytest.fail("Should have gotten a remote assertion error?")


async def spawn_and_error(
    breadth: int,
    depth: int,
) -> None:
    name = tractor.current_actor().name
    async with tractor.open_nursery() as nursery:
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
            await nursery.run_in_actor(*args, **kwargs)


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
    Test that failed actor sets are wrapped in `BaseExceptionGroup`s.

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
    # With `strict=False` the clean-cascade cases (most
    # depth=1 runs, rare depth=3 runs) report as `xpassed`
    # while the race-tripped cases report as `xfailed` —
    # neither flakes `--lf`. When MTF cancel-cascade
    # eventually speeds up enough to close the race even at
    # depth=3, BOTH variants will reliably `xpass` and
    # pytest will yell — our signal to drop the marker. See
    # `ai/conc-anal/cancel_cascade_too_slow_under_main_thread_forkserver_issue.md`.
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

    # headroom for CPU-freq scaling AND/OR slow CI so the inner
    # snapshot-capturing budget doesn't fire spuriously on a
    # sluggish runner; see `cpu_scaling_factor()`.
    from .conftest import cpu_scaling_factor
    timeout *= cpu_scaling_factor()

    async with fail_after_w_trace(timeout):
        try:
            async with tractor.open_nursery() as nursery:
                for i in range(subactor_breadth):
                    await nursery.run_in_actor(
                        spawn_and_error,
                        name=f'spawner_{i}',
                        breadth=subactor_breadth,
                        depth=depth,
                    )
        except BaseExceptionGroup as err:
            assert len(err.exceptions) == subactor_breadth
            for subexc in err.exceptions:

                # verify first level actor errors are wrapped as remote
                if _friggin_windows:

                    # windows is often too slow and cancellation seems
                    # to happen before an actor is spawned
                    if isinstance(subexc, trio.Cancelled):
                        continue

                    elif isinstance(subexc, tractor.RemoteActorError):
                        # on windows it seems we can't exactly be sure wtf
                        # will happen..
                        assert subexc.boxed_type in (
                            tractor.RemoteActorError,
                            trio.Cancelled,
                            BaseExceptionGroup,
                        )

                    elif isinstance(subexc, BaseExceptionGroup):
                        for subsub in subexc.exceptions:

                            if subsub in (tractor.RemoteActorError,):
                                subsub = subsub.boxed_type

                            assert type(subsub) in (
                                trio.Cancelled,
                                BaseExceptionGroup,
                            )
                else:
                    assert isinstance(subexc, tractor.RemoteActorError)

                if depth > 0 and subactor_breadth > 1:
                    # XXX not sure what's up with this..
                    # on windows sometimes spawning is just too slow and
                    # we get back the (sent) cancel signal instead
                    if _friggin_windows:
                        if isinstance(subexc, tractor.RemoteActorError):
                            assert subexc.boxed_type in (
                                BaseExceptionGroup,
                                tractor.RemoteActorError
                            )
                        else:
                            assert isinstance(subexc, BaseExceptionGroup)
                    else:
                        assert subexc.boxed_type is ExceptionGroup
                else:
                    assert subexc.boxed_type in (
                        tractor.RemoteActorError,
                        trio.Cancelled
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
    from .conftest import cpu_scaling_factor

    pid: int = os.getpid()
    timeout: float = (
        4 if _non_linux
        else 2
    )
    if _friggin_windows:  # smh
        timeout += 1

    # add latency headroom for CPU freq scaling (auto-cpufreq et al.)
    headroom: float = cpu_scaling_factor()
    if headroom != 1.:
        timeout *= headroom

    async def spawn_and_sleep_forever(
        task_status=trio.TASK_STATUS_IGNORED
    ):
        async with tractor.open_nursery(
            registry_addrs=[reg_addr],
        ) as tn:
            for i in range(3):
                await tn.run_in_actor(
                    sleep_forever,
                    name='namesucka',
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
        await an.run_in_actor(
            spin_for,
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
            ):
                await an.run_in_actor(
                    spawn_sub_with_sync_blocking_task,
                    name='sync_blocking_sub',
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

    # CPU-scaling / CI latency headroom — macOS CI especially is
    # slow for this graceful-vs-hard-reap timing race; see
    # `cpu_scaling_factor()`.
    from .conftest import cpu_scaling_factor
    timeout *= cpu_scaling_factor()

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
