"""
That "native" debug mode better work!

All these tests can be understood (somewhat) by running the equivalent
`examples/debugging/` scripts manually.

TODO:
    - none of these tests have been run successfully on windows yet but
      there's been manual testing that verified it works.
    - wonder if any of it'll work on OS X?

"""
from os import path
from typing import Optional
import platform
import sys
import time

import pytest
import pexpect
from pexpect.exceptions import (
    TIMEOUT,
    EOF,
)

from conftest import repodir, _ci_env

# TODO: The next great debugger audit could be done by you!
# - recurrent entry to breakpoint() from single actor *after* and an
#   error in another task?
# - root error before child errors
# - root error after child errors
# - root error before child breakpoint
# - root error after child breakpoint
# - recurrent root errors


if platform.system() == 'Windows':
    pytest.skip(
        'Debugger tests have no windows support (yet)',
        allow_module_level=True,
    )


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


# TODO: was trying to this xfail style but some weird bug i see in CI
# that's happening at collect time.. pretty soon gonna dump actions i'm
# thinkin...
# in CI we skip tests which >= depth 1 actor trees due to there
# still being an oustanding issue with relaying the debug-mode-state
# through intermediary parents.
has_nested_actors = pytest.mark.has_nested_actors
# .xfail(
#     os.environ.get('CI', False),
#     reason=(
#         'This test uses nested actors and fails in CI\n'
#         'The test seems to run fine locally but until we solve the '
#         'following issue this CI test will be xfail:\n'
#         'https://github.com/goodboy/tractor/issues/320'
#     )
# )


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


PROMPT = r"\(Pdb\+\+\)"


def expect(
    child,

    # prompt by default
    patt: str = PROMPT,

    **kwargs,

) -> None:
    '''
    Expect wrapper that prints last seen console
    data before failing.

    '''
    try:
        child.expect(
            patt,
            **kwargs,
        )
    except TIMEOUT:
        before = str(child.before.decode())
        print(before)
        raise


def assert_before(
    child,
    patts: list[str],

) -> None:

    before = str(child.before.decode())

    for patt in patts:
        try:
            assert patt in before
        except AssertionError:
            print(before)
            raise


@pytest.fixture(
    params=[False, True],
    ids='ctl-c={}'.format,
)
def ctlc(
    request,
    ci_env: bool,

) -> bool:

    use_ctlc = request.param

    if (
        sys.version_info <= (3, 10)
        and use_ctlc
    ):
        # on 3.9 it seems the REPL UX
        # is highly unreliable and frankly annoying
        # to test for. It does work from manual testing
        # but i just don't think it's wroth it to try
        # and get this working especially since we want to
        # be 3.10+ mega-asap.
        pytest.skip('Py3.9 and `pdbpp` son no bueno..')

    if ci_env:
        node = request.node
        markers = node.own_markers
        for mark in markers:
            if mark.name == 'has_nested_actors':
                pytest.skip(
                    f'Test for {node} uses nested actors and fails in CI\n'
                    f'The test seems to run fine locally but until we solve'
                    'this issue this CI test will be xfail:\n'
                    'https://github.com/goodboy/tractor/issues/320'
                )

    if use_ctlc:
        # XXX: disable pygments highlighting for auto-tests
        # since some envs (like actions CI) will struggle
        # the the added color-char encoding..
        from tractor._debug import TractorConfig
        TractorConfig.use_pygements = False

    yield use_ctlc


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
    expect(child, PROMPT)

    before = str(child.before.decode())

    # make sure expected logging and error arrives
    assert "Attaching to pdb in crashed actor: ('root'" in before
    assert 'AssertionError' in before

    # send user command
    child.sendline(user_input)

    # process should exit
    expect(child, EOF)
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


def do_ctlc(
    child,
    count: int = 3,
    delay: float = 0.1,
    patt: Optional[str] = None,

    # expect repl UX to reprint the prompt after every
    # ctrl-c send.
    # XXX: no idea but, in CI this never seems to work even on 3.10 so
    # needs some further investigation potentially...
    expect_prompt: bool = not _ci_env,

) -> None:

    # make sure ctl-c sends don't do anything but repeat output
    for _ in range(count):
        time.sleep(delay)
        child.sendcontrol('c')

        # TODO: figure out why this makes CI fail..
        # if you run this test manually it works just fine..
        if expect_prompt:
            before = str(child.before.decode())
            time.sleep(delay)
            child.expect(r"\(Pdb\+\+\)")
            time.sleep(delay)

            if patt:
                # should see the last line on console
                assert patt in before


def test_root_actor_bp_forever(
    spawn,
    ctlc: bool,
):
    "Re-enter a breakpoint from the root actor-task."
    child = spawn('root_actor_breakpoint_forever')

    # do some "next" commands to demonstrate recurrent breakpoint
    # entries
    for _ in range(10):

        child.expect(r"\(Pdb\+\+\)")

        if ctlc:
            do_ctlc(child)

        child.sendline('next')

    # do one continue which should trigger a
    # new task to lock the tty
    child.sendline('continue')
    child.expect(r"\(Pdb\+\+\)")

    # seems that if we hit ctrl-c too fast the
    # sigint guard machinery might not kick in..
    time.sleep(0.001)

    if ctlc:
        do_ctlc(child)

    # XXX: this previously caused a bug!
    child.sendline('n')
    child.expect(r"\(Pdb\+\+\)")

    child.sendline('n')
    child.expect(r"\(Pdb\+\+\)")

    # quit out of the loop
    child.sendline('q')
    child.expect(pexpect.EOF)


@pytest.mark.parametrize(
    'do_next',
    (True, False),
    ids='do_next={}'.format,
)
def test_subactor_error(
    spawn,
    ctlc: bool,
    do_next: bool,
):
    '''
    Single subactor raising an error

    '''
    child = spawn('subactor_error')

    # scan for the pdbpp prompt
    child.expect(r"\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "Attaching to pdb in crashed actor: ('name_error'" in before

    if do_next:
        child.sendline('n')

    else:
        # make sure ctl-c sends don't do anything but repeat output
        if ctlc:
            do_ctlc(
                child,
            )

        # send user command and (in this case it's the same for 'continue'
        # vs. 'quit') the debugger should enter a second time in the nursery
        # creating actor
        child.sendline('continue')

    child.expect(r"\(Pdb\+\+\)")
    before = str(child.before.decode())

    # root actor gets debugger engaged
    assert "Attaching to pdb in crashed actor: ('root'" in before
    # error is a remote error propagated from the subactor
    assert "RemoteActorError: ('name_error'" in before

    # another round
    if ctlc:
        do_ctlc(child)

    child.sendline('c')
    child.expect('\r\n')

    # process should exit
    child.expect(pexpect.EOF)


def test_subactor_breakpoint(
    spawn,
    ctlc: bool,
):
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

        if ctlc:
            do_ctlc(child)

    # now run some "continues" to show re-entries
    for _ in range(5):
        child.sendline('continue')
        child.expect(r"\(Pdb\+\+\)")
        before = str(child.before.decode())
        assert "Attaching pdb to actor: ('breakpoint_forever'" in before

        if ctlc:
            do_ctlc(child)

    # finally quit the loop
    child.sendline('q')

    # child process should exit but parent will capture pdb.BdbQuit
    child.expect(r"\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "RemoteActorError: ('breakpoint_forever'" in before
    assert 'bdb.BdbQuit' in before

    if ctlc:
        do_ctlc(child)

    # quit the parent
    child.sendline('c')

    # process should exit
    child.expect(pexpect.EOF)

    before = str(child.before.decode())
    assert "RemoteActorError: ('breakpoint_forever'" in before
    assert 'bdb.BdbQuit' in before


@has_nested_actors
def test_multi_subactors(
    spawn,
    ctlc: bool,
):
    '''
    Multiple subactors, both erroring and
    breakpointing as well as a nested subactor erroring.

    '''
    child = spawn(r'multi_subactors')

    # scan for the pdbpp prompt
    child.expect(r"\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "Attaching pdb to actor: ('breakpoint_forever'" in before

    if ctlc:
        do_ctlc(child)

    # do some "next" commands to demonstrate recurrent breakpoint
    # entries
    for _ in range(10):
        child.sendline('next')
        child.expect(r"\(Pdb\+\+\)")

        if ctlc:
            do_ctlc(child)

    # continue to next error
    child.sendline('c')

    # first name_error failure
    child.expect(r"\(Pdb\+\+\)")
    before = str(child.before.decode())
    assert "Attaching to pdb in crashed actor: ('name_error'" in before
    assert "NameError" in before

    if ctlc:
        do_ctlc(child)

    # continue again
    child.sendline('c')

    # 2nd name_error failure
    child.expect(r"\(Pdb\+\+\)")

    # TODO: will we ever get the race where this crash will show up?
    # blocklist strat now prevents this crash
    # assert_before(child, [
    #     "Attaching to pdb in crashed actor: ('name_error_1'",
    #     "NameError",
    # ])

    if ctlc:
        do_ctlc(child)

    # breakpoint loop should re-engage
    child.sendline('c')
    child.expect(r"\(Pdb\+\+\)")
    before = str(child.before.decode())
    assert "Attaching pdb to actor: ('breakpoint_forever'" in before

    if ctlc:
        do_ctlc(child)

    # wait for spawn error to show up
    spawn_err = "Attaching to pdb in crashed actor: ('spawn_error'"
    start = time.time()
    while (
        spawn_err not in before
        and (time.time() - start) < 3  # timeout eventually
    ):
        child.sendline('c')
        time.sleep(0.1)
        child.expect(r"\(Pdb\+\+\)")
        before = str(child.before.decode())

        if ctlc:
            do_ctlc(child)

    # 2nd depth nursery should trigger
    # (XXX: this below if guard is technically a hack that makes the
    # nested case seem to work locally on linux but ideally in the long
    # run this can be dropped.)
    if not ctlc:
        assert_before(child, [
            spawn_err,
            "RemoteActorError: ('name_error_1'",
        ])

    # now run some "continues" to show re-entries
    for _ in range(5):
        child.sendline('c')
        child.expect(r"\(Pdb\+\+\)")

    # quit the loop and expect parent to attach
    child.sendline('q')
    child.expect(r"\(Pdb\+\+\)")
    before = str(child.before.decode())

    assert_before(child, [
        # debugger attaches to root
        "Attaching to pdb in crashed actor: ('root'",

        # expect a multierror with exceptions for each sub-actor
        "RemoteActorError: ('breakpoint_forever'",
        "RemoteActorError: ('name_error'",
        "RemoteActorError: ('spawn_error'",
        "RemoteActorError: ('name_error_1'",
        'bdb.BdbQuit',
    ])

    if ctlc:
        do_ctlc(child)

    # process should exit
    child.sendline('c')
    child.expect(pexpect.EOF)

    # repeat of previous multierror for final output
    assert_before(child, [
        "RemoteActorError: ('breakpoint_forever'",
        "RemoteActorError: ('name_error'",
        "RemoteActorError: ('spawn_error'",
        "RemoteActorError: ('name_error_1'",
        'bdb.BdbQuit',
    ])


def test_multi_daemon_subactors(
    spawn,
    loglevel: str,
    ctlc: bool
):
    '''
    Multiple daemon subactors, both erroring and breakpointing within a
    stream.

    '''
    child = spawn('multi_daemon_subactors')

    child.expect(r"\(Pdb\+\+\)")

    # there can be a race for which subactor will acquire
    # the root's tty lock first so anticipate either crash
    # message on the first entry.

    bp_forever_msg = "Attaching pdb to actor: ('bp_forever'"
    name_error_msg = "NameError: name 'doggypants' is not defined"

    before = str(child.before.decode())
    if bp_forever_msg in before:
        next_msg = name_error_msg

    elif name_error_msg in before:
        next_msg = bp_forever_msg

    else:
        raise ValueError("Neither log msg was found !?")

    if ctlc:
        do_ctlc(child)

    # NOTE: previously since we did not have clobber prevention
    # in the root actor this final resume could result in the debugger
    # tearing down since both child actors would be cancelled and it was
    # unlikely that `bp_forever` would re-acquire the tty lock again.
    # Now, we should have a final resumption in the root plus a possible
    # second entry by `bp_forever`.

    child.sendline('c')
    child.expect(r"\(Pdb\+\+\)")
    assert_before(child, [next_msg])

    # XXX: hooray the root clobbering the child here was fixed!
    # IMO, this demonstrates the true power of SC system design.

    # now the root actor won't clobber the bp_forever child
    # during it's first access to the debug lock, but will instead
    # wait for the lock to release, by the edge triggered
    # ``_debug.Lock.no_remote_has_tty`` event before sending cancel messages
    # (via portals) to its underlings B)

    # at some point here there should have been some warning msg from
    # the root announcing it avoided a clobber of the child's lock, but
    # it seems unreliable in testing here to gnab it:
    # assert "in use by child ('bp_forever'," in before

    if ctlc:
        do_ctlc(child)

    # expect another breakpoint actor entry
    child.sendline('c')
    child.expect(r"\(Pdb\+\+\)")
    assert_before(child, [bp_forever_msg])

    if ctlc:
        do_ctlc(child)

    # should crash with the 2nd name error (simulates
    # a retry) and then the root eventually (boxed) errors
    # after 1 or more further bp actor entries.

    child.sendline('c')
    child.expect(r"\(Pdb\+\+\)")
    assert_before(child, [name_error_msg])

    # wait for final error in root
    # where it crashs with boxed error
    while True:
        try:
            child.sendline('c')
            child.expect(r"\(Pdb\+\+\)")
            assert_before(
                child,
                [bp_forever_msg]
            )
        except AssertionError:
            break

    # child.sendline('c')
    # assert_before(

    # child.sendline('c')
    assert_before(
        child,
        [
            # boxed error raised in root task
            "Attaching to pdb in crashed actor: ('root'",
            "_exceptions.RemoteActorError: ('name_error'",
        ]
    )

    child.sendline('c')
    child.expect(pexpect.EOF)


@has_nested_actors
def test_multi_subactors_root_errors(
    spawn,
    ctlc: bool
):
    '''
    Multiple subactors, both erroring and breakpointing as well as
    a nested subactor erroring.

    '''
    child = spawn('multi_subactor_root_errors')

    # scan for the pdbpp prompt
    child.expect(r"\(Pdb\+\+\)")

    # at most one subactor should attach before the root is cancelled
    before = str(child.before.decode())
    assert "NameError: name 'doggypants' is not defined" in before

    if ctlc:
        do_ctlc(child)

    # continue again to catch 2nd name error from
    # actor 'name_error_1' (which is 2nd depth).
    child.sendline('c')

    # due to block list strat from #337, this will no longer
    # propagate before the root errors and cancels the spawner sub-tree.
    child.expect(r"\(Pdb\+\+\)")

    # only if the blocking condition doesn't kick in fast enough
    before = str(child.before.decode())
    if "Debug lock blocked for ['name_error_1'" not in before:

        assert_before(child, [
            "Attaching to pdb in crashed actor: ('name_error_1'",
            "NameError",
        ])

        if ctlc:
            do_ctlc(child)

        child.sendline('c')
        child.expect(r"\(Pdb\+\+\)")

    # check if the spawner crashed or was blocked from debug
    # and if this intermediary attached check the boxed error
    before = str(child.before.decode())
    if "Attaching to pdb in crashed actor: ('spawn_error'" in before:

        assert_before(child, [
            # boxed error from spawner's child
            "RemoteActorError: ('name_error_1'",
            "NameError",
        ])

        if ctlc:
            do_ctlc(child)

        child.sendline('c')
        child.expect(r"\(Pdb\+\+\)")

    # expect a root actor crash
    assert_before(child, [
        "RemoteActorError: ('name_error'",
        "NameError",

        # error from root actor and root task that created top level nursery
        "Attaching to pdb in crashed actor: ('root'",
        "AssertionError",
    ])

    child.sendline('c')
    child.expect(pexpect.EOF)

    assert_before(child, [
        # "Attaching to pdb in crashed actor: ('root'",
        # boxed error from previous step
        "RemoteActorError: ('name_error'",
        "NameError",
        "AssertionError",
        'assert 0',
    ])


@has_nested_actors
def test_multi_nested_subactors_error_through_nurseries(
    spawn,

    # TODO: address debugger issue for nested tree:
    # https://github.com/goodboy/tractor/issues/320
    # ctlc: bool,
):
    """Verify deeply nested actors that error trigger debugger entries
    at each actor nurserly (level) all the way up the tree.

    """
    # NOTE: previously, inside this script was a bug where if the
    # parent errors before a 2-levels-lower actor has released the lock,
    # the parent tries to cancel it but it's stuck in the debugger?
    # A test (below) has now been added to explicitly verify this is
    # fixed.

    child = spawn('multi_nested_subactors_error_up_through_nurseries')

    timed_out_early: bool = False

    for i in range(12):
        try:
            child.expect(r"\(Pdb\+\+\)")
            child.sendline('c')
            time.sleep(0.1)

        except EOF:

            # race conditions on how fast the continue is sent?
            print(f"Failed early on {i}?")
            timed_out_early = True
            break
    else:
        child.expect(pexpect.EOF)

    if not timed_out_early:
        before = str(child.before.decode())
        assert "NameError" in before


@pytest.mark.timeout(15)
@has_nested_actors
def test_root_nursery_cancels_before_child_releases_tty_lock(
    spawn,
    start_method,
    ctlc: bool,
):
    '''
    Test that when the root sends a cancel message before a nested child
    has unblocked (which can happen when it has the tty lock and is
    engaged in pdb) it is indeed cancelled after exiting the debugger.

    '''
    timed_out_early = False

    child = spawn('root_cancelled_but_child_is_in_tty_lock')

    child.expect(r"\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "NameError: name 'doggypants' is not defined" in before
    assert "tractor._exceptions.RemoteActorError: ('name_error'" not in before
    time.sleep(0.5)

    if ctlc:
        do_ctlc(child)

    child.sendline('c')

    for i in range(4):
        time.sleep(0.5)
        try:
            child.expect(r"\(Pdb\+\+\)")

        except (
            EOF,
            TIMEOUT,
        ):
            # races all over..

            print(f"Failed early on {i}?")
            before = str(child.before.decode())

            timed_out_early = True

            # race conditions on how fast the continue is sent?
            break

        before = str(child.before.decode())
        assert "NameError: name 'doggypants' is not defined" in before

        if ctlc:
            do_ctlc(child)

        child.sendline('c')
        time.sleep(0.1)

    for i in range(3):
        try:
            child.expect(pexpect.EOF, timeout=0.5)
            break
        except TIMEOUT:
            child.sendline('c')
            time.sleep(0.1)
            print('child was able to grab tty lock again?')
    else:
        print('giving up on child releasing, sending `quit` cmd')
        child.sendline('q')
        expect(child, EOF)

    if not timed_out_early:
        before = str(child.before.decode())
        assert_before(child, [
            "tractor._exceptions.RemoteActorError: ('spawner0'",
            "tractor._exceptions.RemoteActorError: ('name_error'",
            "NameError: name 'doggypants' is not defined",
        ])


def test_root_cancels_child_context_during_startup(
    spawn,
    ctlc: bool,
):
    '''Verify a fast fail in the root doesn't lock up the child reaping
    and all while using the new context api.

    '''
    child = spawn('fast_error_in_root_after_spawn')

    child.expect(r"\(Pdb\+\+\)")

    before = str(child.before.decode())
    assert "AssertionError" in before

    if ctlc:
        do_ctlc(child)

    child.sendline('c')
    child.expect(pexpect.EOF)


def test_different_debug_mode_per_actor(
    spawn,
    ctlc: bool,
):
    child = spawn('per_actor_debug')
    child.expect(r"\(Pdb\+\+\)")

    # only one actor should enter the debugger
    before = str(child.before.decode())
    assert "Attaching to pdb in crashed actor: ('debugged_boi'" in before
    assert "RuntimeError" in before

    if ctlc:
        do_ctlc(child)

    child.sendline('c')
    child.expect(pexpect.EOF)

    before = str(child.before.decode())

    # NOTE: this debugged actor error currently WON'T show up since the
    # root will actually cancel and terminate the nursery before the error
    # msg reported back from the debug mode actor is processed.
    # assert "tractor._exceptions.RemoteActorError: ('debugged_boi'" in before

    assert "tractor._exceptions.RemoteActorError: ('crash_boi'" in before

    # the crash boi should not have made a debugger request but
    # instead crashed completely
    assert "tractor._exceptions.RemoteActorError: ('crash_boi'" in before
    assert "RuntimeError" in before
