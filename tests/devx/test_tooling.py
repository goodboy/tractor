'''
That "native" runtime-hackin toolset better be dang useful!

Verify the funtion of a variety of "developer-experience" tools we
offer from the `.devx` sub-pkg:

- use of the lovely `stackscope` for dumping actor `trio`-task trees
  during operation and hangs.

TODO:
- demonstration of `CallerInfo` call stack frame filtering such that
  for logging and REPL purposes a user sees exactly the layers needed
  when debugging a problem inside the stack vs. in their app.

'''
from __future__ import annotations
from contextlib import (
    contextmanager as cm,
)
import os
import signal
import time
from typing import (
    TYPE_CHECKING,
)

from .conftest import (
    expect,
    assert_before,
    in_prompt_msg,
    PROMPT,
    _pause_msg,
)

import pytest
from pexpect.exceptions import (
    # TIMEOUT,
    EOF,
)

if TYPE_CHECKING:
    from ..conftest import PexpectSpawner


def test_shield_pause(
    spawn: PexpectSpawner,
):
    '''
    Verify the `tractor.pause()/.post_mortem()` API works inside an
    already cancelled `trio.CancelScope` and that you can step to the
    next checkpoint wherein the cancelled will get raised.

    '''
    child = spawn(
        'shield_hang_in_sub'
    )
    expect(
        child,
        'Yo my child hanging..?',
    )
    assert_before(
        child,
        [
            'Entering shield sleep..',
            'Enabling trace-trees on `SIGUSR1` since `stackscope` is installed @',
        ]
    )

    script_pid: int = child.pid
    print(
        f'Sending SIGUSR1 to {script_pid}\n'
        f'(kill -s SIGUSR1 {script_pid})\n'
    )
    os.kill(
        script_pid,
        signal.SIGUSR1,
    )
    time.sleep(0.2)
    expect(
        child,
        # end-of-tree delimiter
        "end-of-\('root'",
    )
    assert_before(
        child,
        [
            # 'Srying to dump `stackscope` tree..',
            # 'Dumping `stackscope` tree for actor',
            "('root'",  # uid line

            # TODO!? this used to show?
            # -[ ] mk reproducable for @oremanj?
            #
            # parent block point (non-shielded)
            # 'await trio.sleep_forever()  # in root',
        ]
    )
    expect(
        child,
        # end-of-tree delimiter
        "end-of-\('hanger'",
    )
    assert_before(
        child,
        [
            # relay to the sub should be reported
            'Relaying `SIGUSR1`[10] to sub-actor',

            "('hanger'",  # uid line

            # TODO!? SEE ABOVE
            # hanger LOC where it's shield-halted
            # 'await trio.sleep_forever()  # in subactor',
        ]
    )

    # simulate the user sending a ctl-c to the hanging program.
    # this should result in the terminator kicking in since
    # the sub is shield blocking and can't respond to SIGINT.
    os.kill(
        child.pid,
        signal.SIGINT,
    )
    from tractor._supervise import _shutdown_msg
    expect(
        child,
        # 'Shutting down actor runtime',
        _shutdown_msg,
        timeout=6,
    )
    assert_before(
        child,
        [
            'raise KeyboardInterrupt',
            # 'Shutting down actor runtime',
            '#T-800 deployed to collect zombie B0',
            "'--uid', \"('hanger',",
        ]
    )


def test_breakpoint_hook_restored(
    spawn: PexpectSpawner,
):
    '''
    Ensures our actor runtime sets a custom `breakpoint()` hook
    on open then restores the stdlib's default on close.

    The hook state validation is done via `assert`s inside the
    invoked script with only `breakpoint()` (not `tractor.pause()`)
    calls used.

    '''
    child = spawn('restore_builtin_breakpoint')

    child.expect(PROMPT)
    try:
        assert_before(
            child,
            [
                _pause_msg,
                "<Task '__main__.main'",
                "('root'",
                "first bp, tractor hook set",
            ]
        )
    # XXX if the above raises `AssertionError`, without sending
    # the final 'continue' cmd to the REPL-active sub-process,
    # we'll hang waiting for that pexpect instance to terminate..
    finally:
        child.sendline('c')

    child.expect(PROMPT)
    assert_before(
        child,
        [
            "last bp, stdlib hook restored",
        ]
    )

    # since the stdlib hook was already restored there should be NO
    # `tractor` `log.pdb()` content from console!
    assert not in_prompt_msg(
        child,
        [
            _pause_msg,
            "<Task '__main__.main'",
            "('root'",
        ],
    )
    child.sendline('c')
    child.expect(EOF)



_to_raise = Exception('Triggering a crash')


@pytest.mark.parametrize(
    'to_raise',
    [
        None,
        _to_raise,
        RuntimeError('Never crash handle this!'),
    ],
)
@pytest.mark.parametrize(
    'raise_on_exit',
    [
        True,
        [type(_to_raise)],
        False,
    ]
)
def test_crash_handler_cms(
    debug_mode: bool,
    to_raise: Exception,
    raise_on_exit: bool|list[Exception],
):
    '''
    Verify the `.devx.open_crash_handler()` API(s) by also
    (conveniently enough) tesing its `repl_fixture: ContextManager`
    param support which for this suite allows use to avoid use of
    a `pexpect`-style-test since we use the fixture to avoid actually
    entering `PdbpREPL.iteract()` :smirk:

    '''
    import tractor
    # import trio

    # state flags
    repl_acquired: bool = False
    repl_released: bool = False

    @cm
    def block_repl_ux(
        repl: tractor.devx.debug.PdbREPL,
        maybe_bxerr: (
            tractor.devx._debug.BoxedMaybeException
            |None
        ) = None,
        enter_repl: bool = True,

    ) -> bool:
        '''
        Set pre/post-REPL state vars and bypass actual conole
        interaction.

        '''
        nonlocal repl_acquired, repl_released

        # task: trio.Task = trio.lowlevel.current_task()
        # print(f'pre-REPL active_task={task.name}')

        print('pre-REPL')
        repl_acquired = True
        yield False  # never actually .interact()
        print('post-REPL')
        repl_released = True

    try:
        # TODO, with runtime's `debug_mode` setting
        # -[ ] need to open runtime tho obvi..
        #
        # with tractor.devx.maybe_open_crash_handler(
        #     pdb=True,

        with tractor.devx.open_crash_handler(
            raise_on_exit=raise_on_exit,
            repl_fixture=block_repl_ux
        ) as bxerr:
            if to_raise is not None:
                raise to_raise

    except Exception as _exc:
        exc = _exc
        if (
            raise_on_exit is True
            or
            type(to_raise) in raise_on_exit
        ):
            assert (
                exc
                is
                to_raise
                is
                bxerr.value
            )

        else:
            raise
    else:
        assert (
            to_raise is None
            or
            not raise_on_exit
            or
            type(to_raise) not in raise_on_exit
        )
        assert bxerr.value is to_raise

    assert bxerr.raise_on_exit == raise_on_exit

    if to_raise is not None:
        assert repl_acquired
        assert repl_released
