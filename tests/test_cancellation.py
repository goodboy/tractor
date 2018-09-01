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


def test_remote_error(arb_addr):
    """Verify an error raises in a subactor is propagated to the parent.
    """
    async def main():
        async with tractor.open_nursery() as nursery:

            portal = await nursery.run_in_actor('errorer', assert_err)

            # get result(s) from main task
            try:
                return await portal.result()
            except tractor.RemoteActorError:
                print("Look Maa that actor failed hard, hehh")
                raise
            except Exception:
                pass
            assert 0, "Remote error was not raised?"

    with pytest.raises(tractor.RemoteActorError):
        # also raises
        tractor.run(main, arbiter_addr=arb_addr)


def do_nothing():
    pass


def test_cancel_single_subactor(arb_addr):

    async def main():

        async with tractor.open_nursery() as nursery:

            portal = await nursery.start_actor(
                'nothin', rpc_module_paths=[__name__],
            )
            assert (await portal.run(__name__, 'do_nothing')) is None

            # would hang otherwise
            await nursery.cancel()

    tractor.run(main, arbiter_addr=arb_addr)


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


@tractor_test
async def test_one_cancels_all():
    """Verify one failed actor causes all others in the nursery
    to be cancelled just like in trio.

    This is the first and only supervisory strategy at the moment.
    """
    try:
        async with tractor.open_nursery() as n:
            real_actors = []
            for i in range(3):
                real_actors.append(await n.start_actor(
                    f'actor_{i}',
                    rpc_module_paths=[__name__],
                ))

            # start one actor that will fail immediately
            await n.run_in_actor('extra', assert_err)

        # should error here with a ``RemoteActorError`` containing
        # an ``AssertionError`

    except tractor.RemoteActorError:
        assert n.cancelled is True
        assert not n._children
    else:
        pytest.fail("Should have gotten a remote assertion error?")
