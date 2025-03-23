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
import os
import signal

from .conftest import (
    expect,
    assert_before,
    in_prompt_msg,
    PROMPT,
    _pause_msg,
)
from pexpect.exceptions import (
    # TIMEOUT,
    EOF,
)


def test_shield_pause(
    spawn,
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

    print(
        'Sending SIGUSR1 to see a tree-trace!',
    )
    os.kill(
        child.pid,
        signal.SIGUSR1,
    )
    expect(
        child,
        # end-of-tree delimiter
        "------ \('root', ",
    )

    assert_before(
        child,
        [
            'Trying to dump `stackscope` tree..',
            'Dumping `stackscope` tree for actor',
            "('root'",  # uid line

            # parent block point (non-shielded)
            'await trio.sleep_forever()  # in root',
        ]
    )

    # expect(
    #     child,
    #     # relay to the sub should be reported
    #     'Relaying `SIGUSR1`[10] to sub-actor',
    # )

    expect(
        child,
        # end-of-tree delimiter
        "------ \('hanger', ",
    )
    assert_before(
        child,
        [
            # relay to the sub should be reported
            'Relaying `SIGUSR1`[10] to sub-actor',

            "('hanger'",  # uid line

            # hanger LOC where it's shield-halted
            'await trio.sleep_forever()  # in subactor',
        ]
    )
    # breakpoint()

    # simulate the user sending a ctl-c to the hanging program.
    # this should result in the terminator kicking in since
    # the sub is shield blocking and can't respond to SIGINT.
    os.kill(
        child.pid,
        signal.SIGINT,
    )
    expect(
        child,
        'Shutting down actor runtime',
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
    spawn,
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
    assert_before(
        child,
        [
            _pause_msg,
            "<Task '__main__.main'",
            "('root'",
            "first bp, tractor hook set",
        ]
    )
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
