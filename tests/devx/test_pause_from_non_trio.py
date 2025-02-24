'''
That "foreign loop/thread" debug REPL support better ALSO WORK!

Same as `test_native_pause.py`.
All these tests can be understood (somewhat) by running the
equivalent `examples/debugging/` scripts manually.

'''
# from functools import partial
# import itertools
import time
# from typing import (
#     Iterator,
# )

import pytest
from pexpect.exceptions import (
    # TIMEOUT,
    EOF,
)

from .conftest import (
    # _ci_env,
    do_ctlc,
    PROMPT,
    # expect,
    in_prompt_msg,
    assert_before,
    _pause_msg,
    _crash_msg,
    _ctlc_ignore_header,
    # _repl_fail_msg,
)


def test_pause_from_sync(
    spawn,
    ctlc: bool,
):
    '''
    Verify we can use the `pdbp` REPL from sync functions AND from
    any thread spawned with `trio.to_thread.run_sync()`.

    `examples/debugging/sync_bp.py`

    '''
    child = spawn('sync_bp')

    # first `sync_pause()` after nurseries open
    child.expect(PROMPT)
    assert_before(
        child,
        [
            # pre-prompt line
            _pause_msg,
            "<Task '__main__.main'",
            "('root'",
        ]
    )
    if ctlc:
        do_ctlc(child)
        # ^NOTE^ subactor not spawned yet; don't need extra delay.

    child.sendline('c')

    # first `await tractor.pause()` inside `p.open_context()` body
    child.expect(PROMPT)

    # XXX shouldn't see gb loaded message with PDB loglevel!
    assert not in_prompt_msg(
        child,
        ['`greenback` portal opened!'],
    )
    # should be same root task
    assert_before(
        child,
        [
            _pause_msg,
            "<Task '__main__.main'",
            "('root'",
        ]
    )

    if ctlc:
        do_ctlc(
            child,
            # NOTE: setting this to 0 (or some other sufficient
            # small val) can cause the test to fail since the
            # `subactor` suffers a race where the root/parent
            # sends an actor-cancel prior to it hitting its pause
            # point; by def the value is 0.1
            delay=0.4,
        )

    # XXX, fwiw without a brief sleep here the SIGINT might actually
    # trigger "subactor" cancellation by its parent  before the
    # shield-handler is engaged.
    #
    # => similar to the `delay` input to `do_ctlc()` below, setting
    # this too low can cause the test to fail since the `subactor`
    # suffers a race where the root/parent sends an actor-cancel
    # prior to the context task hitting its pause point (and thus
    # engaging the `sigint_shield()` handler in time); this value
    # seems be good enuf?
    time.sleep(0.6)

    # one of the bg thread or subactor should have
    # `Lock.acquire()`-ed
    # (NOT both, which will result in REPL clobbering!)
    attach_patts: dict[str, list[str]] = {
        'subactor': [
            "'start_n_sync_pause'",
            "('subactor'",
        ],
        'inline_root_bg_thread': [
            "<Thread(inline_root_bg_thread",
            "('root'",
        ],
        'start_soon_root_bg_thread': [
            "<Thread(start_soon_root_bg_thread",
            "('root'",
        ],
    }
    conts: int = 0  # for debugging below matching logic on failure
    while attach_patts:
        child.sendline('c')
        conts += 1
        child.expect(PROMPT)
        before = str(child.before.decode())
        for key in attach_patts:
            if key in before:
                attach_key: str = key
                expected_patts: str = attach_patts.pop(key)
                assert_before(
                    child,
                    [_pause_msg]
                    +
                    expected_patts
                )
                break
        else:
            pytest.fail(
                f'No keys found?\n\n'
                f'{attach_patts.keys()}\n\n'
                f'{before}\n'
            )

        # ensure no other task/threads engaged a REPL
        # at the same time as the one that was detected above.
        for key, other_patts in attach_patts.copy().items():
            assert not in_prompt_msg(
                child,
                other_patts,
            )

        if ctlc:
            do_ctlc(
                child,
                patt=attach_key,
                # NOTE same as comment above
                delay=0.4,
            )

    child.sendline('c')
    child.expect(EOF)


def expect_any_of(
    attach_patts: dict[str, list[str]],
    child,   # what type?
    ctlc: bool = False,
    prompt: str = _ctlc_ignore_header,
    ctlc_delay: float = .4,

) -> list[str]:
    '''
    Receive any of a `list[str]` of patterns provided in
    `attach_patts`.

    Used to test racing prompts from multiple actors and/or
    tasks using a common root process' `pdbp` REPL.

    '''
    assert attach_patts

    child.expect(PROMPT)
    before = str(child.before.decode())

    for attach_key in attach_patts:
        if attach_key in before:
            expected_patts: str = attach_patts.pop(attach_key)
            assert_before(
                child,
                expected_patts
            )
            break  # from for
    else:
        pytest.fail(
            f'No keys found?\n\n'
            f'{attach_patts.keys()}\n\n'
            f'{before}\n'
        )

    # ensure no other task/threads engaged a REPL
    # at the same time as the one that was detected above.
    for key, other_patts in attach_patts.copy().items():
        assert not in_prompt_msg(
            child,
            other_patts,
        )

    if ctlc:
        do_ctlc(
            child,
            patt=prompt,
            # NOTE same as comment above
            delay=ctlc_delay,
        )

    return expected_patts


def test_sync_pause_from_aio_task(
    spawn,
    ctlc: bool
    # ^TODO, fix for `asyncio`!!
):
    '''
    Verify we can use the `pdbp` REPL from an `asyncio.Task` spawned using
    APIs in `.to_asyncio`.

    `examples/debugging/asycio_bp.py`

    '''
    child = spawn('asyncio_bp')

    # RACE on whether trio/asyncio task bps first
    attach_patts: dict[str, list[str]] = {

        # first pause in guest-mode (aka "infecting")
        # `trio.Task`.
        'trio-side': [
            _pause_msg,
            "<Task 'trio_ctx'",
            "('aio_daemon'",
        ],

        # `breakpoint()` from `asyncio.Task`.
        'asyncio-side': [
            _pause_msg,
            "<Task pending name='Task-2' coro=<greenback_shim()",
            "('aio_daemon'",
        ],
    }

    while attach_patts:
        expect_any_of(
            attach_patts=attach_patts,
            child=child,
            ctlc=ctlc,
        )
        child.sendline('c')

    # NOW in race order,
    # - the asyncio-task will error
    # - the root-actor parent task will pause
    #
    attach_patts: dict[str, list[str]] = {

        # error raised in `asyncio.Task`
        "raise ValueError('asyncio side error!')": [
            _crash_msg,
            'return await chan.receive()',  # `.to_asyncio` impl internals in tb
            "<Task 'trio_ctx'",
            "@ ('aio_daemon'",
            "ValueError: asyncio side error!",
        ],

        # parent-side propagation via actor-nursery/portal
        # "tractor._exceptions.RemoteActorError: remote task raised a 'ValueError'": [
        "remote task raised a 'ValueError'": [
            _crash_msg,
            "src_uid=('aio_daemon'",
            "('aio_daemon'",
        ],

        # a final pause in root-actor
        "<Task '__main__.main'": [
            _pause_msg,
            "<Task '__main__.main'",
            "('root'",
        ],
    }
    while attach_patts:
        expect_any_of(
            attach_patts=attach_patts,
            child=child,
            ctlc=ctlc,
        )
        child.sendline('c')

    assert not attach_patts

    # final boxed error propagates to root
    assert_before(
        child,
        [
            _crash_msg,
            "<Task '__main__.main'",
            "('root'",
            "remote task raised a 'ValueError'",
            "ValueError: asyncio side error!",
        ]
    )

    if ctlc:
        do_ctlc(
            child,
            # NOTE: setting this to 0 (or some other sufficient
            # small val) can cause the test to fail since the
            # `subactor` suffers a race where the root/parent
            # sends an actor-cancel prior to it hitting its pause
            # point; by def the value is 0.1
            delay=0.4,
        )

    child.sendline('c')
    child.expect(EOF)


def test_sync_pause_from_non_greenbacked_aio_task():
    '''
    Where the `breakpoint()` caller task is NOT spawned by
    `tractor.to_asyncio` and thus never activates
    a `greenback.ensure_portal()` beforehand, presumably bc the task
    was started by some lib/dep as in often seen in the field.

    Ensure sync pausing works when the pause is in,

    - the root actor running in infected-mode?
      |_ since we don't need any IPC to acquire the debug lock?
      |_ is there some way to handle this like the non-main-thread case?

    All other cases need to error out appropriately right?

    - for any subactor we can't avoid needing the repl lock..
      |_ is there a way to hook into `asyncio.ensure_future(obj)`?

    '''
    pass
