"""
That "native" debug mode better work!

All these tests can be understood (somewhat) by running the equivalent
`examples/debugging/` scripts manually.

TODO:
    - none of these tests have been run successfully on windows yet but
      there's been manual testing that verified it works.
    - wonder if any of it'll work on OS X?

"""
from __future__ import annotations
from functools import partial
import itertools
import platform
import time
from typing import (
    TYPE_CHECKING,
)

import pytest
from pexpect.exceptions import (
    TIMEOUT,
    EOF,
)

from .conftest import (
    do_ctlc,
    PROMPT,
    _pause_msg,
    _crash_msg,
    _repl_fail_msg,
)
from .conftest import (
    expect,
    in_prompt_msg,
    assert_before,
)

if TYPE_CHECKING:
    from ..conftest import PexpectSpawner

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


@pytest.mark.parametrize(
    'user_in_out',
    [
        ('c', 'AssertionError'),
        ('q', 'AssertionError'),
    ],
    ids=lambda item: f'{item[0]} -> {item[1]}',
)
def test_root_actor_error(
    spawn,
    user_in_out,
):
    '''
    Demonstrate crash handler entering pdb from basic error in root actor.

    '''
    user_input, expect_err_str = user_in_out

    child = spawn('root_actor_error')

    # scan for the prompt
    expect(child, PROMPT)

    # make sure expected logging and error arrives
    assert in_prompt_msg(
        child,
        [
            _crash_msg,
            "('root'",
            'AssertionError',
        ]
    )

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
    '''
    Demonstrate breakpoint from in root actor.

    '''
    user_input, expect_err_str = user_in_out
    child = spawn('root_actor_breakpoint')

    # scan for the prompt
    child.expect(PROMPT)

    assert 'Error' not in str(child.before)

    # send user command
    child.sendline(user_input)
    child.expect('\r\n')

    # process should exit
    child.expect(EOF)

    if expect_err_str is None:
        assert 'Error' not in str(child.before)
    else:
        assert expect_err_str in str(child.before)


def test_root_actor_bp_forever(
    spawn,
    ctlc: bool,
):
    "Re-enter a breakpoint from the root actor-task."
    child = spawn('root_actor_breakpoint_forever')

    # do some "next" commands to demonstrate recurrent breakpoint
    # entries
    for _ in range(10):

        child.expect(PROMPT)

        if ctlc:
            do_ctlc(child)

        child.sendline('next')

    # do one continue which should trigger a
    # new task to lock the tty
    child.sendline('continue')
    child.expect(PROMPT)

    # seems that if we hit ctrl-c too fast the
    # sigint guard machinery might not kick in..
    time.sleep(0.001)

    if ctlc:
        do_ctlc(child)

    # XXX: this previously caused a bug!
    child.sendline('n')
    child.expect(PROMPT)

    child.sendline('n')
    child.expect(PROMPT)

    # quit out of the loop
    child.sendline('q')
    child.expect(EOF)


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

    # scan for the prompt
    child.expect(PROMPT)

    assert in_prompt_msg(
        child,
        [
            _crash_msg,
            "('name_error'",
        ]
    )

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

    child.expect(PROMPT)
    assert in_prompt_msg(
        child,
        [
            _crash_msg,
            # root actor gets debugger engaged
            "('root'",
            # error is a remote error propagated from the subactor
            "('name_error'",
        ]
    )

    # another round
    if ctlc:
        do_ctlc(child)

    child.sendline('c')
    child.expect('\r\n')

    # process should exit
    child.expect(EOF)


def test_subactor_breakpoint(
    spawn,
    ctlc: bool,
):
    "Single subactor with an infinite breakpoint loop"

    child = spawn('subactor_breakpoint')
    child.expect(PROMPT)
    assert in_prompt_msg(
        child,
        [_pause_msg,
         "('breakpoint_forever'",]
    )

    # do some "next" commands to demonstrate recurrent breakpoint
    # entries
    for _ in range(10):
        child.sendline('next')
        child.expect(PROMPT)

        if ctlc:
            do_ctlc(child)

    # now run some "continues" to show re-entries
    for _ in range(5):
        child.sendline('continue')
        child.expect(PROMPT)
        assert in_prompt_msg(
            child,
            [_pause_msg, "('breakpoint_forever'"]
        )

        if ctlc:
            do_ctlc(child)

    # finally quit the loop
    child.sendline('q')

    # child process should exit but parent will capture pdb.BdbQuit
    child.expect(PROMPT)

    assert in_prompt_msg(
        child,
        ['RemoteActorError:',
         "('breakpoint_forever'",
         'bdb.BdbQuit',]
    )

    if ctlc:
        do_ctlc(child)

    # quit the parent
    child.sendline('c')

    # process should exit
    child.expect(EOF)

    assert in_prompt_msg(
        child, [
        'MessagingError:',
        'RemoteActorError:',
         "('breakpoint_forever'",
         'bdb.BdbQuit',
        ],
        pause_on_false=True,
    )


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

    # scan for the prompt
    child.expect(PROMPT)

    before = str(child.before.decode())
    assert in_prompt_msg(
        child,
        [_pause_msg, "('breakpoint_forever'"]
    )

    if ctlc:
        do_ctlc(child)

    # do some "next" commands to demonstrate recurrent breakpoint
    # entries
    for _ in range(10):
        child.sendline('next')
        child.expect(PROMPT)

        if ctlc:
            do_ctlc(child)

    # continue to next error
    child.sendline('c')

    # first name_error failure
    child.expect(PROMPT)
    assert in_prompt_msg(
        child,
        [
            _crash_msg,
            "('name_error'",
            "NameError",
        ]
    )

    if ctlc:
        do_ctlc(child)

    # continue again
    child.sendline('c')

    # 2nd name_error failure
    child.expect(PROMPT)

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
    child.expect(PROMPT)
    assert in_prompt_msg(
        child,
        [_pause_msg, "('breakpoint_forever'"]
    )

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
        child.expect(PROMPT)
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
        child.expect(PROMPT)

    # quit the loop and expect parent to attach
    child.sendline('q')
    child.expect(PROMPT)
    before = str(child.before.decode())

    assert_before(
        child, [
            # debugger attaches to root
            # "Attaching to pdb in crashed actor: ('root'",
            _crash_msg,
            "('root'",

            # expect a multierror with exceptions for each sub-actor
            "RemoteActorError: ('breakpoint_forever'",
            "RemoteActorError: ('name_error'",
            "RemoteActorError: ('spawn_error'",
            "RemoteActorError: ('name_error_1'",
            'bdb.BdbQuit',
        ]
    )

    if ctlc:
        do_ctlc(child)

    # process should exit
    child.sendline('c')
    child.expect(EOF)

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

    child.expect(PROMPT)

    # there can be a race for which subactor will acquire
    # the root's tty lock first so anticipate either crash
    # message on the first entry.

    bp_forev_parts = [
        _pause_msg,
        "('bp_forever'",
    ]
    bp_forev_in_msg = partial(
        in_prompt_msg,
        parts=bp_forev_parts,
    )

    name_error_msg: str = "NameError: name 'doggypants' is not defined"
    name_error_parts: list[str] = [name_error_msg]

    before = str(child.before.decode())

    if bp_forev_in_msg(child=child):
        next_parts = name_error_parts

    elif name_error_msg in before:
        next_parts = bp_forev_parts

    else:
        raise ValueError('Neither log msg was found !?')

    if ctlc:
        do_ctlc(child)

    # NOTE: previously since we did not have clobber prevention
    # in the root actor this final resume could result in the debugger
    # tearing down since both child actors would be cancelled and it was
    # unlikely that `bp_forever` would re-acquire the tty lock again.
    # Now, we should have a final resumption in the root plus a possible
    # second entry by `bp_forever`.

    child.sendline('c')
    child.expect(PROMPT)
    assert_before(
        child,
        next_parts,
    )

    # XXX: hooray the root clobbering the child here was fixed!
    # IMO, this demonstrates the true power of SC system design.

    # now the root actor won't clobber the bp_forever child
    # during it's first access to the debug lock, but will instead
    # wait for the lock to release, by the edge triggered
    # ``devx.debug.Lock.no_remote_has_tty`` event before sending cancel messages
    # (via portals) to its underlings B)

    # at some point here there should have been some warning msg from
    # the root announcing it avoided a clobber of the child's lock, but
    # it seems unreliable in testing here to gnab it:
    # assert "in use by child ('bp_forever'," in before

    if ctlc:
        do_ctlc(child)

    # expect another breakpoint actor entry
    child.sendline('c')
    child.expect(PROMPT)

    try:
        assert_before(
            child,
            bp_forev_parts,
        )
    except AssertionError:
        assert_before(
            child,
            name_error_parts,
        )

    else:
        if ctlc:
            do_ctlc(child)

        # should crash with the 2nd name error (simulates
        # a retry) and then the root eventually (boxed) errors
        # after 1 or more further bp actor entries.

        child.sendline('c')
        child.expect(PROMPT)
        assert_before(
            child,
            name_error_parts,
        )

    # wait for final error in root
    # where it crashs with boxed error
    while True:
        child.sendline('c')
        child.expect(PROMPT)
        if not in_prompt_msg(
            child,
            bp_forev_parts
        ):
            break

    assert_before(
        child,
        [
            # boxed error raised in root task
            # "Attaching to pdb in crashed actor: ('root'",
            _crash_msg,
            "('root'",  # should attach in root
            "_exceptions.RemoteActorError:",  # with an embedded RAE for..
            "('name_error'",  # the src subactor which raised
        ]
    )

    child.sendline('c')
    child.expect(EOF)


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

    # scan for the prompt
    child.expect(PROMPT)

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
    child.expect(PROMPT)

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
        child.expect(PROMPT)

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
        child.expect(PROMPT)

    # expect a root actor crash
    assert_before(child, [
        "RemoteActorError: ('name_error'",
        "NameError",

        # error from root actor and root task that created top level nursery
        "Attaching to pdb in crashed actor: ('root'",
        "AssertionError",
    ])

    child.sendline('c')
    child.expect(EOF)

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
    '''
    Verify deeply nested actors that error trigger debugger entries
    at each actor nurserly (level) all the way up the tree.

    '''
    # NOTE: previously, inside this script was a bug where if the
    # parent errors before a 2-levels-lower actor has released the lock,
    # the parent tries to cancel it but it's stuck in the debugger?
    # A test (below) has now been added to explicitly verify this is
    # fixed.

    child = spawn('multi_nested_subactors_error_up_through_nurseries')

    # timed_out_early: bool = False

    for send_char in itertools.cycle(['c', 'q']):
        try:
            child.expect(PROMPT)
            child.sendline(send_char)
            time.sleep(0.01)

        except EOF:
            break

    assert_before(
        child,
        [ # boxed source errors
            "NameError: name 'doggypants' is not defined",
            "tractor._exceptions.RemoteActorError:",
            "('name_error'",
            "bdb.BdbQuit",

            # first level subtrees
            # "tractor._exceptions.RemoteActorError: ('spawner0'",
            "src_uid=('spawner0'",

            # "tractor._exceptions.RemoteActorError: ('spawner1'",

            # propagation of errors up through nested subtrees
            # "tractor._exceptions.RemoteActorError: ('spawn_until_0'",
            # "tractor._exceptions.RemoteActorError: ('spawn_until_1'",
            # "tractor._exceptions.RemoteActorError: ('spawn_until_2'",
            # ^-NOTE-^ old RAE repr, new one is below with a field
            # showing the src actor's uid.
            "src_uid=('spawn_until_0'",
            "relay_uid=('spawn_until_1'",
            "src_uid=('spawn_until_2'",
        ]
    )


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

    child.expect(PROMPT)
    assert_before(
        child,
        [
            "NameError: name 'doggypants' is not defined",
            "tractor._exceptions.RemoteActorError: ('name_error'",
        ],
    )
    time.sleep(0.5)

    if ctlc:
        do_ctlc(child)

    child.sendline('c')

    for i in range(4):
        time.sleep(0.5)
        try:
            child.expect(PROMPT)

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
            child.expect(EOF, timeout=0.5)
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
        assert_before(
            child,
            [
                "tractor._exceptions.RemoteActorError: ('spawner0'",
                "tractor._exceptions.RemoteActorError: ('name_error'",
                "NameError: name 'doggypants' is not defined",
            ],
        )


def test_root_cancels_child_context_during_startup(
    spawn,
    ctlc: bool,
):
    '''Verify a fast fail in the root doesn't lock up the child reaping
    and all while using the new context api.

    '''
    child = spawn('fast_error_in_root_after_spawn')

    child.expect(PROMPT)

    before = str(child.before.decode())
    assert "AssertionError" in before

    if ctlc:
        do_ctlc(child)

    child.sendline('c')
    child.expect(EOF)


def test_different_debug_mode_per_actor(
    spawn,
    ctlc: bool,
):
    child = spawn('per_actor_debug')
    child.expect(PROMPT)

    # only one actor should enter the debugger
    assert in_prompt_msg(
        child,
        [_crash_msg, "('debugged_boi'", "RuntimeError"],
    )

    if ctlc:
        do_ctlc(child)

    child.sendline('c')
    child.expect(EOF)

    # NOTE: this debugged actor error currently WON'T show up since the
    # root will actually cancel and terminate the nursery before the error
    # msg reported back from the debug mode actor is processed.
    # assert "tractor._exceptions.RemoteActorError: ('debugged_boi'" in before

    # the crash boi should not have made a debugger request but
    # instead crashed completely
    assert_before(
        child,
        [
            "tractor._exceptions.RemoteActorError:",
            "src_uid=('crash_boi'",
            "RuntimeError",
        ]
    )


def test_post_mortem_api(
    spawn,
    ctlc: bool,
):
    '''
    Verify the `tractor.post_mortem()` API works in an exception
    handler block.

    '''
    child = spawn('pm_in_subactor')

    # First entry is via manual `.post_mortem()`
    child.expect(PROMPT)
    assert_before(
        child,
        [
            _crash_msg,
            "<Task 'name_error'",
            "NameError",
            "('child'",
            "tractor.post_mortem()",
        ]
    )
    if ctlc:
        do_ctlc(child)
    child.sendline('c')

    # 2nd is RPC crash handler
    child.expect(PROMPT)
    assert_before(
        child,
        [
            _crash_msg,
            "<Task 'name_error'",
            "NameError",
            "('child'",
        ]
    )
    if ctlc:
        do_ctlc(child)
    child.sendline('c')

    # 3rd is via RAE bubbled to root's parent ctx task and
    # crash-handled via another manual pm call.
    child.expect(PROMPT)
    assert_before(
        child,
        [
            _crash_msg,
            "<Task '__main__.main'",
            "('root'",
            "NameError",
            "tractor.post_mortem()",
            "src_uid=('child'",
        ]
    )
    if ctlc:
        do_ctlc(child)
    child.sendline('c')

    # 4th and FINAL is via RAE bubbled to root's parent ctx task and
    # crash-handled via another manual pm call.
    child.expect(PROMPT)
    assert_before(
        child,
        [
            _crash_msg,
            "<Task '__main__.main'",
            "('root'",
            "NameError",
            "src_uid=('child'",
        ]
    )
    if ctlc:
        do_ctlc(child)


    # TODO: ensure we're stopped and showing the right call stack frame
    # -[ ] need a way to strip the terminal color chars in order to
    #    pattern match... see TODO around `assert_before()` above!
    # child.sendline('w')
    # child.expect(PROMPT)
    # assert_before(
    #     child,
    #     [
    #         # error src block annot at ctx open
    #         '-> async with p.open_context(name_error) as (ctx, first):',
    #     ]
    # )

    # # step up a frame to ensure the it's the root's nursery
    # child.sendline('u')
    # child.expect(PROMPT)
    # assert_before(
    #     child,
    #     [
    #         # handler block annotation
    #         '-> async with tractor.open_nursery(',
    #     ]
    # )

    child.sendline('c')
    child.expect(EOF)


def test_shield_pause(
    spawn,
):
    '''
    Verify the `tractor.pause()/.post_mortem()` API works inside an
    already cancelled `trio.CancelScope` and that you can step to the
    next checkpoint wherein the cancelled will get raised.

    '''
    child = spawn('shielded_pause')

    # First entry is via manual `.post_mortem()`
    child.expect(PROMPT)
    assert_before(
        child,
        [
            _pause_msg,
            "cancellable_pause_loop'",
            "('cancelled_before_pause'",  # actor name
        ]
    )

    # since 3 tries in ex. shield pause loop
    for i in range(3):
        child.sendline('c')
        child.expect(PROMPT)
        assert_before(
            child,
            [
                _pause_msg,
                "INSIDE SHIELDED PAUSE",
                "('cancelled_before_pause'",  # actor name
            ]
        )

    # back inside parent task that opened nursery
    child.sendline('c')
    child.expect(PROMPT)
    assert_before(
        child,
        [
            _crash_msg,
            "('cancelled_before_pause'",  # actor name
            _repl_fail_msg,
            "trio.Cancelled",
            "raise Cancelled._create()",

            # we should be handling a taskc inside
            # the first `.port_mortem()` sin-shield!
            'await DebugStatus.req_finished.wait()',
        ]
    )

    # same as above but in the root actor's task
    child.sendline('c')
    child.expect(PROMPT)
    assert_before(
        child,
        [
            _crash_msg,
            "('root'",  # actor name
            _repl_fail_msg,
            "trio.Cancelled",
            "raise Cancelled._create()",

            # handling a taskc inside the first unshielded
            # `.port_mortem()`.
            # BUT in this case in the root-proc path ;)
            'wait Lock._debug_lock.acquire()',
        ]
    )
    child.sendline('c')
    child.expect(EOF)


@pytest.mark.parametrize(
    'quit_early', [False, True]
)
def test_ctxep_pauses_n_maybe_ipc_breaks(
    spawn: PexpectSpawner,
    quit_early: bool,
):
    '''
    Audit generator embedded `.pause()`es from within a `@context`
    endpoint with a chan close at the end, requiring that ctl-c is
    mashed and zombie reaper kills sub with no hangs.

    '''
    child = spawn('subactor_bp_in_ctx')
    child.expect(PROMPT)

    # 3 iters for the `gen()` pause-points
    for i in range(3):
        assert_before(
            child,
            [
                _pause_msg,
                "('bp_boi'",  # actor name
                "<Task 'just_bp'",  # task name
            ]
        )
        if (
            i == 1
            and
            quit_early
        ):
            child.sendline('q')
            child.expect(PROMPT)
            assert_before(
                child,
                ["tractor._exceptions.RemoteActorError: remote task raised a 'BdbQuit'",
                 "bdb.BdbQuit",
                 "('bp_boi'",
                ]
            )
            child.sendline('c')
            child.expect(EOF)
            assert_before(
                child,
                ["tractor._exceptions.RemoteActorError: remote task raised a 'BdbQuit'",
                 "bdb.BdbQuit",
                 "('bp_boi'",
                ]
            )
            break  # end-of-test

        child.sendline('c')
        try:
            child.expect(PROMPT)
        except TIMEOUT:
            # no prompt since we hang due to IPC chan purposely
            # closed so verify we see error reporting as well as
            # a failed crash-REPL request msg and can CTL-c our way
            # out.
            assert_before(
                child,
                ['peer IPC channel closed abruptly?',
                 'another task closed this fd',
                 'Debug lock request was CANCELLED?',
                 "TransportClosed: 'MsgpackUDSStream' was already closed locally ?",]

                # XXX races on whether these show/hit?
                 # 'Failed to REPl via `_pause()` You called `tractor.pause()` from an already cancelled scope!',
                 # 'AssertionError',
            )
            # OSc(ancel) the hanging tree
            do_ctlc(
                child=child,
                expect_prompt=False,
            )
            child.expect(EOF)
            assert_before(
                child,
                ['KeyboardInterrupt'],
            )


# TODO: better error for "non-ideal" usage from the root actor.
# -[ ] if called from an async scope emit a message that suggests
#    using `await tractor.pause()` instead since it's less overhead
#    (in terms of `greenback` and/or extra threads) and if it's from
#    a sync scope suggest that usage must first call
#    `ensure_portal()` in the (eventual parent) async calling scope?
def test_sync_pause_from_bg_task_in_root_actor_():
    '''
    When used from the root actor, normally we can only implicitly
    support `.pause_from_sync()` from the main-parent-task (that
    opens the runtime via `open_root_actor()`) since `greenback`
    requires a `.ensure_portal()` call per `trio.Task` where it is
    used.

    '''
    ...

# TODO: needs ANSI code stripping tho, see `assert_before()` # above!
def test_correct_frames_below_hidden():
    '''
    Ensure that once a `tractor.pause()` enages, when the user
    inputs a "next"/"n" command the actual next line steps
    and that using a "step"/"s" into the next LOC, particuarly
    `tractor` APIs, you can step down into that code.

    '''
    ...


def test_cant_pause_from_paused_task():
    '''
    Pausing from with an already paused task should raise an error.

    Normally this should only happen in practise while debugging the call stack of `tractor.pause()` itself, likely
    by a `.pause()` line somewhere inside our runtime.

    '''
    ...
