"""
Cancellation and error propagation

"""
import os
import signal
import platform
import time
from itertools import repeat

import pytest
import trio
import tractor
from tractor._testing import (
    tractor_test,
)
from .conftest import no_windows


def is_win():
    return platform.system() == 'Windows'


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
def test_remote_error(reg_addr, args_err):
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


@pytest.mark.parametrize('delay', (0, 0.5))
@pytest.mark.parametrize(
    'num_subactors', range(25, 26),
)
def test_multierror_fast_nursery(reg_addr, start_method, num_subactors, delay):
    """Verify we raise a ``BaseExceptionGroup`` out of a nursery where
    more then one actor errors and also with a delay before failure
    to test failure during an ongoing spawning.
    """
    async def main():
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
    with pytest.raises(BaseExceptionGroup) as exc_info:
        trio.run(main)

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


@pytest.mark.parametrize('mechanism', ['nursery_cancel', KeyboardInterrupt])
def test_cancel_single_subactor(reg_addr, mechanism):
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


@tractor_test
async def test_cancel_infinite_streamer(start_method):

    # stream for at most 1 seconds
    with trio.move_on_after(1) as cancel_scope:
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
    assert n.cancelled


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
@tractor_test
async def test_some_cancels_all(
    num_actors_and_errs: tuple,
    start_method: str,
    loglevel: str,
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

        assert an.cancelled is True
        assert not an._children
    else:
        pytest.fail("Should have gotten a remote assertion error?")


async def spawn_and_error(breadth, depth) -> None:
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


@tractor_test
async def test_nested_multierrors(loglevel, start_method):
    '''
    Test that failed actor sets are wrapped in `BaseExceptionGroup`s. This
    test goes only 2 nurseries deep but we should eventually have tests
    for arbitrary n-depth actor trees.

    '''
    if start_method == 'trio':
        depth = 3
        subactor_breadth = 2
    else:
        # XXX: multiprocessing can't seem to handle any more then 2 depth
        # process trees for whatever reason.
        # Any more process levels then this and we see bugs that cause
        # hangs and broken pipes all over the place...
        if start_method == 'forkserver':
            pytest.skip("Forksever sux hard at nested spawning...")
        depth = 1  # means an additional actor tree of spawning (2 levels deep)
        subactor_breadth = 2

    with trio.fail_after(120):
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
                if is_win():

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
                    if is_win():
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
    loglevel,
    start_method,
    spawn_backend,
):
    """Ensure that a control-C (SIGINT) signal cancels both the parent and
    child processes in trionic fashion
    """
    pid = os.getpid()

    async def main():
        with trio.fail_after(2):
            async with tractor.open_nursery() as tn:
                await tn.start_actor('sucka')
                if 'mp' in spawn_backend:
                    time.sleep(0.1)
                os.kill(pid, signal.SIGINT)
                await trio.sleep_forever()

    with pytest.raises(KeyboardInterrupt):
        trio.run(main)


@no_windows
def test_cancel_via_SIGINT_other_task(
    loglevel,
    start_method,
    spawn_backend,
):
    """Ensure that a control-C (SIGINT) signal cancels both the parent
    and child processes in trionic fashion even a subprocess is started
    from a seperate ``trio`` child  task.
    """
    pid = os.getpid()
    timeout: float = 2
    if is_win():  # smh
        timeout += 1

    async def spawn_and_sleep_forever(
        task_status=trio.TASK_STATUS_IGNORED
    ):
        async with tractor.open_nursery() as tn:
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
            async with (

                # XXX ?TODO? why no work!?
                # tractor.trionics.collapse_eg(),
                trio.open_nursery(
                    strict_exception_groups=False,
                ) as tn,
            ):
                await tn.start(spawn_and_sleep_forever)
                if 'mp' in spawn_backend:
                    time.sleep(0.1)
                os.kill(pid, signal.SIGINT)

    with pytest.raises(KeyboardInterrupt):
        trio.run(main)


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
    spawn_backend: str,
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
        pytest.skip("Forksever sux hard at resuming from sync sleep...")

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
        delay = 4  # is AssertionError in eg AND no _cs cancellation.

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
    start_method,
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

    if is_win():  # smh
        timeout += 1

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
