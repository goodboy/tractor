"""
That native debug better work!
"""
from os import path

import pytest
import pexpect

from .test_docs_examples import repodir


def examples_dir():
    """Return the abspath to the examples directory.
    """
    return path.join(repodir(), 'examples', 'debugging/')


def mk_cmd(ex_name: str) -> str:
    """Generate a command suitable to pass to ``pexpect.spawn()``.
    """
    return ' '.join(
        ['python',
         path.join(examples_dir(), f'{ex_name}.py')]
    )


@pytest.fixture
def spawn(
    testdir,
    arb_addr,
) -> pexpect.spawn:

    def _spawn(cmd):
        return testdir.spawn(
            cmd=mk_cmd(cmd),
            expect_timeout=3,
        )

    return _spawn


@pytest.mark.parametrize(
    'user_in_out',
    [
        ('c', 'AssertionError'),
        ('q', 'AssertionError'),
    ],
    ids=lambda item: item[1],
)
def test_root_actor_error(spawn, user_in_out):
    """Demonstrate crash handler entering pdbpp from basic error in root actor.
    """
    user_input, expect_err_str = user_in_out

    child = spawn('root_actor_error')

    # scan for the pdbpp prompt
    child.expect("\(Pdb\+\+\)")

    # make sure expected logging and error arrives
    assert 'TTY lock acquired' in str(child.before)
    assert 'AssertionError' in str(child.before)

    # send user command
    child.sendline(user_input)
    child.expect('\r\n')
    child.expect('TTY lock released')

    # process should exit
    child.expect(pexpect.EOF)
    assert expect_err_str in str(child.before)


@pytest.mark.parametrize(
    'user_in_out',
    [
        ('c', None),
        ('q', 'bdb.BdbQuit'),
    ],
    ids=lambda item: f'{item[0]} -> {item[1]}',
)
def test_root_actor_bp(spawn, user_in_out):
    """Demonstrate breakpoint from in root actor.
    """
    user_input, expect_err_str = user_in_out
    child = spawn('root_actor_breakpoint')

    # scan for the pdbpp prompt
    child.expect("\(Pdb\+\+\)")

    assert 'Error' not in str(child.before)

    # send user command
    child.sendline(user_input)
    child.expect('\r\n')

    # process should exit
    child.expect(pexpect.EOF)

    if expect_err_str is None:
        assert 'Error' not in str(child.before)
    else:
        assert expect_err_str in str(child.before)


def test_subactor_error(spawn):
    child = spawn('subactor_error')

    # scan for the pdbpp prompt
    child.expect("\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "Attaching to pdb in crashed actor: ('name_error'" in before

    # send user command
    # (in this case it's the same for 'continue' vs. 'quit')
    child.sendline('continue')
    child.expect('\r\n')

    # the debugger should enter a second time in the nursery
    # creating actor

    child.expect("\(Pdb\+\+\)")

    before = str(child.before.decode())

    # root actor gets debugger engaged
    assert "Attaching to pdb in crashed actor: ('arbiter'" in before

    # error is a remote error propagated from the subactor
    assert "RemoteActorError: ('name_error'" in before

    child.sendline('c')
    child.expect('\r\n')

    # process should exit
    child.expect(pexpect.EOF)


def test_subactor_breakpoint(spawn):
    child = spawn('subactor_breakpoint')

    # scan for the pdbpp prompt
    child.expect("\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "Attaching pdb to actor: ('breakpoint_forever'" in before

    # do some "next" commands to demonstrate recurrent breakpoint
    # entries
    for _ in range(10):
        child.sendline('next')
        child.expect("\(Pdb\+\+\)")

    # now run some "continues" to show re-entries
    for _ in range(5):
        child.sendline('continue')
        child.expect("\(Pdb\+\+\)")
        before = str(child.before.decode())
        assert "Attaching pdb to actor: ('breakpoint_forever'" in before

    # finally quit the loop
    child.sendline('q')

    # child process should exit but parent will capture pdb.BdbQuit
    child.expect("\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "RemoteActorError: ('breakpoint_forever'" in before
    assert 'bdb.BdbQuit' in before

    # quit the parent
    child.sendline('c')

    # process should exit
    child.expect(pexpect.EOF)

    before = str(child.before.decode())
    assert "RemoteActorError: ('breakpoint_forever'" in before
    assert 'bdb.BdbQuit' in before
