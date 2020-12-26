"""
That native debug better work!

All these tests can be understood (somewhat) by running the equivalent
`examples/debugging/` scripts manually.

TODO: None of these tests have been run successfully on windows yet.
"""
import time
from os import path

import pytest
import pexpect

from conftest import repodir


# TODO: The next great debugger audit could be done by you!
# - recurrent entry to breakpoint() from single actor *after* and an
#   error in another task?
# - root error before child errors
# - root error after child errors
# - root error before child breakpoint
# - root error after child breakpoint
# - recurrent root errors


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
    start_method,
    testdir,
    arb_addr,
) -> 'pexpect.spawn':

    if start_method != 'trio':
        pytest.skip(
            "Debugger tests are only supported on the trio backend"
        )

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
    ids=lambda item: f'{item[0]} -> {item[1]}',
)
def test_root_actor_error(spawn, user_in_out):
    """Demonstrate crash handler entering pdbpp from basic error in root actor.
    """
    user_input, expect_err_str = user_in_out

    child = spawn('root_actor_error')

    # scan for the pdbpp prompt
    child.expect(r"\(Pdb\+\+\)")

    before = str(child.before.decode())

    # make sure expected logging and error arrives
    assert "Attaching to pdb in crashed actor: ('arbiter'" in before
    assert 'AssertionError' in before

    # send user command
    child.sendline(user_input)

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
    child.expect(r"\(Pdb\+\+\)")

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


def test_root_actor_bp_forever(spawn):
    "Re-enter a breakpoint from the root actor-task."
    child = spawn('root_actor_breakpoint_forever')

    # do some "next" commands to demonstrate recurrent breakpoint
    # entries
    for _ in range(10):
        child.sendline('next')
        child.expect(r"\(Pdb\+\+\)")

    # do one continue which should trigger a new task to lock the tty
    child.sendline('continue')
    child.expect(r"\(Pdb\+\+\)")

    # XXX: this previously caused a bug!
    child.sendline('n')
    child.expect(r"\(Pdb\+\+\)")

    child.sendline('n')
    child.expect(r"\(Pdb\+\+\)")


def test_subactor_error(spawn):
    "Single subactor raising an error"

    child = spawn('subactor_error')

    # scan for the pdbpp prompt
    child.expect(r"\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "Attaching to pdb in crashed actor: ('name_error'" in before

    # send user command
    # (in this case it's the same for 'continue' vs. 'quit')
    child.sendline('continue')

    # the debugger should enter a second time in the nursery
    # creating actor

    child.expect(r"\(Pdb\+\+\)")

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
    "Single subactor with an infinite breakpoint loop"

    child = spawn('subactor_breakpoint')

    # scan for the pdbpp prompt
    child.expect(r"\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "Attaching pdb to actor: ('breakpoint_forever'" in before

    # do some "next" commands to demonstrate recurrent breakpoint
    # entries
    for _ in range(10):
        child.sendline('next')
        child.expect(r"\(Pdb\+\+\)")

    # now run some "continues" to show re-entries
    for _ in range(5):
        child.sendline('continue')
        child.expect(r"\(Pdb\+\+\)")
        before = str(child.before.decode())
        assert "Attaching pdb to actor: ('breakpoint_forever'" in before

    # finally quit the loop
    child.sendline('q')

    # child process should exit but parent will capture pdb.BdbQuit
    child.expect(r"\(Pdb\+\+\)")

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


def test_multi_subactors(spawn):
    """Multiple subactors, both erroring and breakpointing as well as
    a nested subactor erroring.
    """
    child = spawn(r'multi_subactors')

    # scan for the pdbpp prompt
    child.expect(r"\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "Attaching pdb to actor: ('breakpoint_forever'" in before

    # do some "next" commands to demonstrate recurrent breakpoint
    # entries
    for _ in range(10):
        child.sendline('next')
        child.expect(r"\(Pdb\+\+\)")

    # continue to next error
    child.sendline('c')

    # first name_error failure
    child.expect(r"\(Pdb\+\+\)")
    before = str(child.before.decode())
    assert "NameError" in before

    # continue again
    child.sendline('c')

    # 2nd name_error failure
    child.expect(r"\(Pdb\+\+\)")
    before = str(child.before.decode())
    assert "NameError" in before

    # breakpoint loop should re-engage
    child.sendline('c')
    child.expect(r"\(Pdb\+\+\)")
    before = str(child.before.decode())
    assert "Attaching pdb to actor: ('breakpoint_forever'" in before

    # now run some "continues" to show re-entries
    for _ in range(5):
        child.sendline('c')
        child.expect(r"\(Pdb\+\+\)")

    # quit the loop and expect parent to attach
    child.sendline('q')
    child.expect(r"\(Pdb\+\+\)")
    before = str(child.before.decode())
    assert "Attaching to pdb in crashed actor: ('arbiter'" in before
    assert "RemoteActorError: ('breakpoint_forever'" in before
    assert 'bdb.BdbQuit' in before

    # process should exit
    child.sendline('c')
    child.expect(pexpect.EOF)

    before = str(child.before.decode())
    assert "RemoteActorError: ('breakpoint_forever'" in before
    assert 'bdb.BdbQuit' in before


def test_multi_daemon_subactors(spawn, loglevel):
    """Multiple daemon subactors, both erroring and breakpointing within a
    stream.
    """
    child = spawn('multi_daemon_subactors')

    child.expect(r"\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "Attaching pdb to actor: ('bp_forever'" in before

    child.sendline('c')

    # first name_error failure
    child.expect(r"\(Pdb\+\+\)")
    before = str(child.before.decode())
    assert "NameError" in before

    child.sendline('c')

    child.expect(r"\(Pdb\+\+\)")
    before = str(child.before.decode())
    assert "tractor._exceptions.RemoteActorError: ('name_error'" in before

    try:
        child.sendline('c')
        child.expect(pexpect.EOF)
    except pexpect.exceptions.TIMEOUT:
        # Failed to exit using continue..?

        child.sendline('q')
        child.expect(pexpect.EOF)



def test_multi_subactors_root_errors(spawn):
    """Multiple subactors, both erroring and breakpointing as well as
    a nested subactor erroring.
    """
    child = spawn('multi_subactor_root_errors')

    # scan for the pdbpp prompt
    child.expect(r"\(Pdb\+\+\)")

    # at most one subactor should attach before the root is cancelled
    before = str(child.before.decode())
    assert "NameError: name 'doggypants' is not defined" in before

    # continue again
    child.sendline('c')
    child.expect(r"\(Pdb\+\+\)")

    # should now get attached in root with assert error
    before = str(child.before.decode())

    # should have come just after priot prompt
    assert "Attaching to pdb in crashed actor: ('arbiter'" in before
    assert "AssertionError" in before

    # warnings assert we probably don't need
    # assert "Cancelling nursery in ('spawn_error'," in before

    # continue again
    child.sendline('c')
    child.expect(pexpect.EOF)

    before = str(child.before.decode())
    assert "AssertionError" in before


def test_multi_nested_subactors_error_through_nurseries(spawn):
    """Verify deeply nested actors that error trigger debugger entries
    at each actor nurserly (level) all the way up the tree.
    """

    # NOTE: previously, inside this script was a a bug where if the
    # parent errors before a 2-levels-lower actor has released the lock,
    # the parent tries to cancel it but it's stuck in the debugger?
    # A test (below) has now been added to explicitly verify this is
    # fixed.

    child = spawn('multi_nested_subactors_error_up_through_nurseries')

    # startup time can be iffy
    time.sleep(1)

    for i in range(12):
        try:
            child.expect(r"\(Pdb\+\+\)")
            child.sendline('c')
            time.sleep(0.1)

        except (
            pexpect.exceptions.EOF,
            pexpect.exceptions.TIMEOUT,
        ):
            # races all over..

            print(f"Failed early on {i}?")
            before = str(child.before.decode())

            timed_out_early = True

            # race conditions on how fast the continue is sent?
            break

    child.expect(pexpect.EOF)

    if not timed_out_early:
        before = str(child.before.decode())
        assert "NameError" in before


def test_root_nursery_cancels_before_child_releases_tty_lock(spawn, start_method):
    """Test that when the root sends a cancel message before a nested
    child has unblocked (which can happen when it has the tty lock and
    is engaged in pdb) it is indeed cancelled after exiting the debugger.
    """
    timed_out_early = False

    child = spawn('root_cancelled_but_child_is_in_tty_lock')

    child.expect(r"\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "NameError: name 'doggypants' is not defined" in before
    assert "tractor._exceptions.RemoteActorError: ('name_error'" not in before
    time.sleep(0.5)

    child.sendline('c')


    for i in range(4):
        time.sleep(0.5)
        try:
            child.expect(r"\(Pdb\+\+\)")

        except (
            pexpect.exceptions.EOF,
            pexpect.exceptions.TIMEOUT,
        ):
            # races all over..

            print(f"Failed early on {i}?")
            before = str(child.before.decode())

            timed_out_early = True

            # race conditions on how fast the continue is sent?
            break


        before = str(child.before.decode())
        assert "NameError: name 'doggypants' is not defined" in before

        child.sendline('c')

    child.expect(pexpect.EOF)

    if not timed_out_early:

        before = str(child.before.decode())
        assert "tractor._exceptions.RemoteActorError: ('spawner0'" in before
        assert "tractor._exceptions.RemoteActorError: ('name_error'" in before
        assert "NameError: name 'doggypants' is not defined" in before
