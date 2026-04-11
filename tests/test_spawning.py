"""
Spawning basics including audit of,

- subproc bootstrap, such as subactor runtime-data/config inheritance,
- basic (and mostly legacy) `ActorNursery` subactor starting and
  cancel APIs.

Simple (and generally legacy) examples from the original
API design.

"""
from functools import partial
from typing import (
    Any,
)

import pytest
import trio
import tractor

from tractor._testing import tractor_test

data_to_pass_down = {
    'doggy': 10,
    'kitty': 4,
}


async def spawn(
    should_be_root: bool,
    data: dict,
    reg_addr: tuple[str, int],

    debug_mode: bool = False,
):
    await trio.sleep(0.1)
    actor = tractor.current_actor(err_on_no_runtime=False)

    if should_be_root:
        assert actor is None  # no runtime yet
        async with (
            tractor.open_root_actor(
                arbiter_addr=reg_addr,
            ),
            tractor.open_nursery() as an,
        ):
            # now runtime exists
            actor: tractor.Actor = tractor.current_actor()
            assert actor.is_registrar == should_be_root

            # spawns subproc here
            portal: tractor.Portal = await an.run_in_actor(
                fn=spawn,

                # spawning args
                name='sub-actor',
                enable_modules=[__name__],

                # passed to a subactor-recursive RPC invoke
                # of this same `spawn()` fn.
                should_be_root=False,
                data=data_to_pass_down,
                reg_addr=reg_addr,
            )

            assert len(an._children) == 1
            assert (
                portal.channel.uid
                in
                tractor.current_actor().ipc_server._peers
            )

            # get result from child subactor
            result = await portal.result()
            assert result == 10
            return result
    else:
        assert actor.is_registrar == should_be_root
        return 10


def test_run_in_actor_same_func_in_child(
    reg_addr: tuple,
    debug_mode: bool,
):
    result = trio.run(
        partial(
            spawn,
            should_be_root=True,
            data=data_to_pass_down,
            reg_addr=reg_addr,
            debug_mode=debug_mode,
        )
    )
    assert result == 10


async def movie_theatre_question():
    '''
    A question asked in a dark theatre, in a tangent
    (errr, I mean different) process.

    '''
    return 'have you ever seen a portal?'


@tractor_test
async def test_movie_theatre_convo(
    start_method: str,
):
    '''
    The main ``tractor`` routine.

    '''
    async with tractor.open_nursery(debug_mode=True) as an:

        portal = await an.start_actor(
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
        ) as an:
            portal = await an.run_in_actor(
                cellar_door,
                return_value=return_value,
                name='some_linguist',
            )

            res: Any = await portal.wait_for_result()
            assert res == return_value
    # The ``async with`` will unblock here since the 'some_linguist'
    # actor has completed its main task ``cellar_door``.

    # this should pull the cached final result already captured during
    # the nursery block exit.
    res: Any = await portal.wait_for_result()
    assert res == return_value
    print(res)


async def check_loglevel(level):
    assert tractor.current_actor().loglevel == level
    log = tractor.log.get_logger()
    # XXX using a level actually used inside tractor seems to trigger
    # some kind of `logging` module bug FYI.
    log.critical('yoyoyo')


@pytest.mark.parametrize(
    'level', [
        'debug',
        'cancel',
        'critical'
    ],
    ids='loglevel={}'.format,
)
def test_loglevel_propagated_to_subactor(
    capfd: pytest.CaptureFixture,
    start_method: str,
    reg_addr: tuple,
    level: str,
):
    if start_method == 'mp_forkserver':
        pytest.skip(
            "a bug with `capfd` seems to make forkserver capture not work?"
        )

    async def main():
        async with tractor.open_nursery(
            name='registrar',
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


async def check_parent_main_inheritance(
    expect_inherited: bool,
) -> bool:
    '''
    Assert that the child actor's ``_parent_main_data`` matches the
    ``inherit_parent_main`` flag it was spawned with.

    With the trio spawn backend the parent's ``__main__`` bootstrap
    data is captured and forwarded to each child so it can replay
    the parent's ``__main__`` as ``__mp_main__``, mirroring the
    stdlib ``multiprocessing`` bootstrap:
    https://docs.python.org/3/library/multiprocessing.html#the-spawn-and-forkserver-start-methods

    When ``inherit_parent_main=False`` the data dict is empty
    (``{}``) so no fixup ever runs and the child keeps its own
    ``__main__`` untouched.

    NOTE: under `pytest` the parent ``__main__`` is
    ``pytest.__main__`` whose ``_fixup_main_from_name()`` is a no-op
    (the name ends with ``.__main__``), so we cannot observe
    a difference in ``sys.modules['__main__'].__name__`` between the
    two modes.  Checking ``_parent_main_data`` directly is the most
    reliable verification that the flag is threaded through
    correctly; a ``RemoteActorError[AssertionError]`` propagates on
    mismatch.

    '''
    import tractor
    actor: tractor.Actor = tractor.current_actor()
    has_data: bool = bool(actor._parent_main_data)
    assert has_data == expect_inherited, (
        f'Expected _parent_main_data to be '
        f'{"non-empty" if expect_inherited else "empty"}, '
        f'got: {actor._parent_main_data!r}'
    )
    return has_data


def test_run_in_actor_can_skip_parent_main_inheritance(
    start_method: str,  # <- only support on `trio` backend rn.
):
    '''
    Verify ``inherit_parent_main=False`` on ``run_in_actor()``
    prevents parent ``__main__`` data from reaching the child.

    '''
    if start_method != 'trio':
        pytest.skip(
            'parent main-inheritance opt-out only affects the trio backend'
        )

    async def main():
        async with tractor.open_nursery(start_method='trio') as an:

            # Default: child receives parent __main__ bootstrap data
            replaying = await an.run_in_actor(
                check_parent_main_inheritance,
                name='replaying-parent-main',
                expect_inherited=True,
            )
            await replaying.result()

            # Opt-out: child gets no parent __main__ data
            isolated = await an.run_in_actor(
                check_parent_main_inheritance,
                name='isolated-parent-main',
                inherit_parent_main=False,
                expect_inherited=False,
            )
            await isolated.result()

    trio.run(main)


def test_start_actor_can_skip_parent_main_inheritance(
    start_method: str,  # <- only support on `trio` backend rn.
):
    '''
    Verify ``inherit_parent_main=False`` on ``start_actor()``
    prevents parent ``__main__`` data from reaching the child.

    '''
    if start_method != 'trio':
        pytest.skip(
            'parent main-inheritance opt-out only affects the trio backend'
        )

    async def main():
        async with tractor.open_nursery(start_method='trio') as an:

            # Default: child receives parent __main__ bootstrap data
            replaying = await an.start_actor(
                'replaying-parent-main',
                enable_modules=[__name__],
            )
            result = await replaying.run(
                check_parent_main_inheritance,
                expect_inherited=True,
            )
            assert result is True
            await replaying.cancel_actor()

            # Opt-out: child gets no parent __main__ data
            isolated = await an.start_actor(
                'isolated-parent-main',
                enable_modules=[__name__],
                inherit_parent_main=False,
            )
            result = await isolated.run(
                check_parent_main_inheritance,
                expect_inherited=False,
            )
            assert result is False
            await isolated.cancel_actor()

    trio.run(main)
