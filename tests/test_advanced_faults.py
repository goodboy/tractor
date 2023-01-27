'''
Sketchy network blackoutz, ugly byzantine gens, puedes eschuchar la
cancelacion?..

'''
import pytest
from _pytest.pathlib import import_path
import trio

from conftest import (
    examples_dir,
)


@pytest.mark.parametrize(
    'debug_mode',
    [False, True],
    ids=['debug_mode', 'no_debug_mode'],
)
def test_child_breaks_ipc_channel_during_stream(
    debug_mode: bool,
):
    '''
    Ensure we can (purposely) break IPC during streaming and it's still
    possible for the (simulated) user to kill the actor tree using
    SIGINT.

    '''
    mod = import_path(
        examples_dir() / 'advanced_faults' / 'ipc_failure_during_stream.py',
        root=examples_dir(),
    )

    with pytest.raises(KeyboardInterrupt):
        trio.run(
            mod.main,
            debug_mode,
        )
