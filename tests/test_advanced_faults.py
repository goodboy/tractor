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
        # no breaks
        {
            'break_parent_ipc_after': False,
            'break_child_ipc_after': False,
        },

        # only parent breaks
        {
            'break_parent_ipc_after': 500,
            'break_child_ipc_after': False,
        },

        # only child breaks
        {
            'break_parent_ipc_after': False,
            'break_child_ipc_after': 500,
        },

        # both: break parent first
        {
            'break_parent_ipc_after': 500,
            'break_child_ipc_after': 800,
        },
        # both: break child first
        {
            'break_parent_ipc_after': 800,
            'break_child_ipc_after': 500,
        },

    ],
    ids=[
        'no_break',
        'break_parent',
        'break_child',
        'break_both_parent_first',
        'break_both_child_first',
    ],
)
def test_ipc_channel_break_during_stream(
    debug_mode: bool,
    spawn_backend: str,
    ipc_break: dict | None,
):
    '''
    Ensure we can have an IPC channel break its connection during
    streaming and it's still possible for the (simulated) user to kill
    the actor tree using SIGINT.

    We also verify the type of connection error expected in the parent
    depending on which side if the IPC breaks first.

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
    if (

        # only child breaks
        (
            ipc_break['break_child_ipc_after']
            and ipc_break['break_parent_ipc_after'] is False
        )

        # both break but, parent breaks first
        or (
            ipc_break['break_child_ipc_after'] is not False
            and (
                ipc_break['break_parent_ipc_after']
                > ipc_break['break_child_ipc_after']
            )
        )

    ):
        expect_final_exc = trio.ClosedResourceError

    # when the parent IPC side dies (even if the child's does as well
    # but the child fails BEFORE the parent) we expect the channel to be
    # sent a stop msg from the child at some point which will signal the
    # parent that the stream has been terminated.
    # NOTE: when the parent breaks "after" the child you get this same
    # case as well, the child breaks the IPC channel with a stop msg
    # before any closure takes place.
    elif (
        # only parent breaks
        (
            ipc_break['break_parent_ipc_after']
            and ipc_break['break_child_ipc_after'] is False
        )

        # both break but, child breaks first
        or (
            ipc_break['break_parent_ipc_after'] is not False
            and (
                ipc_break['break_child_ipc_after']
                > ipc_break['break_parent_ipc_after']
            )
        )
    ):
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
