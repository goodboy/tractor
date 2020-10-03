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


def spawn(
    cmd: str,
    testdir,
) -> pexpect.spawn:
    return testdir.spawn(
        cmd=mk_cmd(cmd),
        expect_timeout=3,
    )


@pytest.mark.parametrize(
    'user_in_out',
    [
        ('c', 'AssertionError'),
        ('q', 'AssertionError'),
    ],
    ids=lambda item: item[1],
)
def test_root_actor_error(testdir, user_in_out):
    """Demonstrate crash handler entering pdbpp from basic error in root actor.
    """
    user_input, expect_err_str = user_in_out

    child = spawn('root_actor_error', testdir)

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
def test_root_actor_bp(testdir, user_in_out):
    """Demonstrate breakpoint from in root actor.
    """
    user_input, expect_err_str = user_in_out
    child = spawn('root_actor_breakpoint', testdir)

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


def test_subactor_error(testdir):
    ...


def test_subactor_breakpoint(testdir):
    ...
