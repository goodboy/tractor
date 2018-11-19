"""
Cancellation and error propagation
"""
from itertools import repeat

import pytest
import trio
import tractor

from conftest import tractor_test


async def assert_err():
    assert 0


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
def test_remote_error(arb_addr, args_err):
    """Verify an error raised in a subactor that is propagated
    to the parent nursery, contains underlying builtin erorr type
    infot and causes cancellation and reraising.
    """
    args, errtype = args_err

    async def main():
        async with tractor.open_nursery() as nursery:

            portal = await nursery.run_in_actor('errorer', assert_err, **args)

            # get result(s) from main task
            try:
                await portal.result()
            except tractor.RemoteActorError as err:
                assert err.type == errtype
                print("Look Maa that actor failed hard, hehh")
                raise
            else:
                assert 0, "Remote error was not raised?"

    with pytest.raises(tractor.RemoteActorError):
        # also raises
        tractor.run(main, arbiter_addr=arb_addr)


def test_multierror(arb_addr):
    """Verify we raise a ``trio.MultiError`` out of a nursery where
    more then one actor errors.
    """
    async def main():
        async with tractor.open_nursery() as nursery:

            await nursery.run_in_actor('errorer1', assert_err)
            portal2 = await nursery.run_in_actor('errorer2', assert_err)

            # get result(s) from main task
            try:
                await portal2.result()
            except tractor.RemoteActorError as err:
                assert err.type == AssertionError
                print("Look Maa that first actor failed hard, hehh")
                raise

        # here we should get a `trio.MultiError` containing exceptions
        # from both subactors

    with pytest.raises(trio.MultiError):
        tractor.run(main, arbiter_addr=arb_addr)


def do_nothing():
    pass


def test_cancel_single_subactor(arb_addr):
    """Ensure a ``ActorNursery.start_actor()`` spawned subactor
    cancels when the nursery is cancelled.
    """
    async def spawn_actor():
        """Spawn an actor that blocks indefinitely.
        """
        async with tractor.open_nursery() as nursery:

            portal = await nursery.start_actor(
                'nothin', rpc_module_paths=[__name__],
            )
            assert (await portal.run(__name__, 'do_nothing')) is None

            # would hang otherwise
            await nursery.cancel()

    tractor.run(spawn_actor, arbiter_addr=arb_addr)


async def stream_forever():
    for i in repeat("I can see these little future bubble things"):
        # each yielded value is sent over the ``Channel`` to the
        # parent actor
        yield i
        await trio.sleep(0.01)


@tractor_test
async def test_cancel_infinite_streamer():

    # stream for at most 1 seconds
    with trio.move_on_after(1) as cancel_scope:
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                f'donny',
                rpc_module_paths=[__name__],
            )

            # this async for loop streams values from the above
            # async generator running in a separate process
            async for letter in await portal.run(__name__, 'stream_forever'):
                print(letter)

    # we support trio's cancellation system
    assert cancel_scope.cancelled_caught
    assert n.cancelled


@pytest.mark.parametrize(
    'num_actors_and_errs',
    [
        (1, tractor.RemoteActorError, AssertionError),
        (2, tractor.MultiError, AssertionError)
    ],
    ids=['one_actor', 'two_actors'],
)
@tractor_test
async def test_some_cancels_all(num_actors_and_errs):
    """Verify one failed actor causes all others in the nursery
    to be cancelled just like in trio.

    This is the first and only supervisory strategy at the moment.
    """
    num, first_err, err_type = num_actors_and_errs
    try:
        async with tractor.open_nursery() as n:
            real_actors = []
            for i in range(3):
                real_actors.append(await n.start_actor(
                    f'actor_{i}',
                    rpc_module_paths=[__name__],
                ))

            for i in range(num):
                # start one actor that will fail immediately
                await n.run_in_actor(f'extra_{i}', assert_err)

        # should error here with a ``RemoteActorError`` or
        # ``MultiError`` containing an ``AssertionError`

    except first_err as err:
        if isinstance(err, tractor.MultiError):
            assert len(err.exceptions) == num
            for exc in err.exceptions:
                if isinstance(exc, tractor.RemoteActorError):
                    assert exc.type == err_type
                else:
                    assert isinstance(exc, trio.Cancelled)
        elif isinstance(err, tractor.RemoteActorError):
            assert err.type == err_type

        assert n.cancelled is True
        assert not n._children
    else:
        pytest.fail("Should have gotten a remote assertion error?")
