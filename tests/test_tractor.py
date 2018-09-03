"""
Actor model API testing
"""
import time

import pytest
import trio
import tractor

from conftest import tractor_test


@pytest.mark.trio
async def test_no_arbitter():
    """An arbitter must be established before any nurseries
    can be created.

    (In other words ``tractor.run`` must be used instead of ``trio.run`` as is
    done by the ``pytest-trio`` plugin.)
    """
    with pytest.raises(RuntimeError):
        with tractor.open_nursery():
            pass


def test_local_actor_async_func(arb_addr):
    """Verify a simple async function in-process.
    """
    nums = []

    async def print_loop():
        # arbiter is started in-proc if dne
        assert tractor.current_actor().is_arbiter

        for i in range(10):
            nums.append(i)
            await trio.sleep(0.1)

    start = time.time()
    tractor.run(print_loop, arbiter_addr=arb_addr)

    # ensure the sleeps were actually awaited
    assert time.time() - start >= 1
    assert nums == list(range(10))


statespace = {'doggy': 10, 'kitty': 4}


async def spawn(is_arbiter):
    namespaces = [__name__]

    await trio.sleep(0.1)
    actor = tractor.current_actor()
    assert actor.is_arbiter == is_arbiter
    assert actor.statespace == statespace

    if actor.is_arbiter:
        async with tractor.open_nursery() as nursery:
            # forks here
            portal = await nursery.run_in_actor(
                'sub-actor',
                spawn,
                is_arbiter=False,
                statespace=statespace,
                rpc_module_paths=namespaces,
            )

            assert len(nursery._children) == 1
            assert portal.channel.uid in tractor.current_actor()._peers
            # be sure we can still get the result
            result = await portal.result()
            assert result == 10
            return result
    else:
        return 10


def test_local_arbiter_subactor_global_state(arb_addr):
    result = tractor.run(
        spawn,
        True,
        name='arbiter',
        statespace=statespace,
        arbiter_addr=arb_addr,
    )
    assert result == 10


def movie_theatre_question():
    """A question asked in a dark theatre, in a tangent
    (errr, I mean different) process.
    """
    return 'have you ever seen a portal?'


@tractor_test
async def test_movie_theatre_convo():
    """The main ``tractor`` routine.
    """
    async with tractor.open_nursery() as n:

        portal = await n.start_actor(
            'frank',
            # enable the actor to run funcs from this current module
            rpc_module_paths=[__name__],
        )

        print(await portal.run(__name__, 'movie_theatre_question'))
        # call the subactor a 2nd time
        print(await portal.run(__name__, 'movie_theatre_question'))

        # the async with will block here indefinitely waiting
        # for our actor "frank" to complete, we cancel 'frank'
        # to avoid blocking indefinitely
        await portal.cancel_actor()


def cellar_door():
    return "Dang that's beautiful"


@tractor_test
async def test_most_beautiful_word():
    """The main ``tractor`` routine.
    """
    async with tractor.open_nursery() as n:

        portal = await n.run_in_actor('some_linguist', cellar_door)

    # The ``async with`` will unblock here since the 'some_linguist'
    # actor has completed its main task ``cellar_door``.

    print(await portal.result())
