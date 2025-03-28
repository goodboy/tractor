"""
Spawning basics

"""
from typing import (
    Any,
)

import pytest
import trio
import tractor

from tractor._testing import tractor_test

data_to_pass_down = {'doggy': 10, 'kitty': 4}


async def spawn(
    is_arbiter: bool,
    data: dict,
    reg_addr: tuple[str, int],
):
    namespaces = [__name__]

    await trio.sleep(0.1)

    async with tractor.open_root_actor(
        arbiter_addr=reg_addr,
    ):
        actor = tractor.current_actor()
        assert actor.is_arbiter == is_arbiter
        data = data_to_pass_down

        if actor.is_arbiter:
            async with tractor.open_nursery() as nursery:

                # forks here
                portal = await nursery.run_in_actor(
                    spawn,
                    is_arbiter=False,
                    name='sub-actor',
                    data=data,
                    reg_addr=reg_addr,
                    enable_modules=namespaces,
                )

                assert len(nursery._children) == 1
                assert portal.channel.uid in tractor.current_actor()._peers
                # be sure we can still get the result
                result = await portal.result()
                assert result == 10
                return result
        else:
            return 10


def test_local_arbiter_subactor_global_state(
    reg_addr,
):
    result = trio.run(
        spawn,
        True,
        data_to_pass_down,
        reg_addr,
    )
    assert result == 10


async def movie_theatre_question():
    """A question asked in a dark theatre, in a tangent
    (errr, I mean different) process.
    """
    return 'have you ever seen a portal?'


@tractor_test
async def test_movie_theatre_convo(start_method):
    """The main ``tractor`` routine.
    """
    async with tractor.open_nursery() as n:

        portal = await n.start_actor(
            'frank',
            # enable the actor to run funcs from this current module
            enable_modules=[__name__],
        )

        print(await portal.run(movie_theatre_question))
        # call the subactor a 2nd time
        print(await portal.run(movie_theatre_question))

        # the async with will block here indefinitely waiting
        # for our actor "frank" to complete, we cancel 'frank'
        # to avoid blocking indefinitely
        await portal.cancel_actor()


async def cellar_door(
    return_value: str|None,
):
    return return_value


@pytest.mark.parametrize(
    'return_value', ["Dang that's beautiful", None],
    ids=['return_str', 'return_None'],
)
@tractor_test
async def test_most_beautiful_word(
    start_method: str,
    return_value: Any,
    debug_mode: bool,
):
    '''
    The main ``tractor`` routine.

    '''
    with trio.fail_after(1):
        async with tractor.open_nursery(
            debug_mode=debug_mode,
        ) as n:
            portal = await n.run_in_actor(
                cellar_door,
                return_value=return_value,
                name='some_linguist',
            )

            print(await portal.result())
    # The ``async with`` will unblock here since the 'some_linguist'
    # actor has completed its main task ``cellar_door``.

    # this should pull the cached final result already captured during
    # the nursery block exit.
    print(await portal.result())


async def check_loglevel(level):
    assert tractor.current_actor().loglevel == level
    log = tractor.log.get_logger()
    # XXX using a level actually used inside tractor seems to trigger
    # some kind of `logging` module bug FYI.
    log.critical('yoyoyo')


def test_loglevel_propagated_to_subactor(
    start_method,
    capfd,
    reg_addr,
):
    if start_method == 'mp_forkserver':
        pytest.skip(
            "a bug with `capfd` seems to make forkserver capture not work?")

    level = 'critical'

    async def main():
        async with tractor.open_nursery(
            name='arbiter',
            start_method=start_method,
            arbiter_addr=reg_addr,

        ) as tn:
            await tn.run_in_actor(
                check_loglevel,
                loglevel=level,
                level=level,
            )

    trio.run(main)

    # ensure subactor spits log message on stderr
    captured = capfd.readouterr()
    assert 'yoyoyo' in captured.err
