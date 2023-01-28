'''
Sketchy network blackoutz, ugly byzantine gens, puedes eschuchar la
cancelacion?..

'''
from functools import partial

import pytest
from _pytest.pathlib import import_path
import trio

from conftest import (
    examples_dir,
)


@pytest.mark.parametrize(
    'debug_mode',
    [False, True],
    ids=['no_debug_mode', 'debug_mode'],
)
@pytest.mark.parametrize(
    'ipc_break',
    [
        {},
        {'break_parent_ipc': True},
        {'break_child_ipc': True},
        {
            'break_child_ipc': True,
            'break_parent_ipc': True,
        },
    ],
    ids=[
        'no_break',
        'break_parent',
        'break_child',
        'break_both',
    ],
)
def test_child_breaks_ipc_channel_during_stream(
    debug_mode: bool,
    spawn_backend: str,
    ipc_break: dict | None,
):
    '''
    Ensure we can (purposely) break IPC during streaming and it's still
    possible for the (simulated) user to kill the actor tree using
    SIGINT.

    '''
    if spawn_backend != 'trio':
        if debug_mode:
            pytest.skip('`debug_mode` only supported on `trio` spawner')

        # non-`trio` spawners should never hit the hang condition that
        # requires the user to do ctl-c to cancel the actor tree.
        expect_final_exc = trio.ClosedResourceError

    mod = import_path(
        examples_dir() / 'advanced_faults' / 'ipc_failure_during_stream.py',
        root=examples_dir(),
    )

    expect_final_exc = KeyboardInterrupt

    # when ONLY the child breaks we expect the parent to get a closed
    # resource error on the next `MsgStream.receive()` and then fail out
    # and cancel the child from there.
    if 'break_child_ipc' in ipc_break:
        expect_final_exc = trio.ClosedResourceError

    # when the parent IPC side dies (even if the child's does as well)
    # we expect the channel to be sent a stop msg from the child at some
    # point which will signal the parent that the stream has been
    # terminated.
    # NOTE: when the parent breaks "after" the child you get the above
    # case as well, but it's not worth testing right?
    if 'break_parent_ipc' in ipc_break:
        expect_final_exc = trio.EndOfChannel

    with pytest.raises(expect_final_exc):
        trio.run(
            partial(
                mod.main,
                debug_mode=debug_mode,
                start_method=spawn_backend,
                **ipc_break,
            )
        )
