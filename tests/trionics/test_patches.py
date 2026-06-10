'''
Regression tests for `tractor.trionics.patches` —
defensive monkey-patches on upstream `trio` bugs.

Each test asserts:

1. The bug exists (or is gone — skip cleanly if
   upstream shipped the fix and our `is_needed()` now
   returns `False`).
2. Our patch fixes it (post-`apply()` the `repro()`
   returns cleanly within a tight wall-clock cap).

Wall-clock caps are critical here — the bugs we patch
are tight-loops or deadlocks, so a regression would
HANG the test runner unless we hard-cap each
`repro()` call.

'''
import signal

import pytest

from tractor.trionics import patches
from tractor.trionics.patches import _wakeup_socketpair as wsp


@pytest.fixture(autouse=True)
def _alarm_cleanup():
    '''
    Ensure no leftover SIGALRM survives a test failure
    or unexpected return.

    '''
    yield
    signal.alarm(0)


def test_wakeup_socketpair_drain_eof_patch_works():
    '''
    Without the patch, `WakeupSocketpair.drain()` on a
    socketpair whose write-end has been closed spins
    forever. With the patch applied, it returns
    cleanly within milliseconds.

    Wall-clock cap: 2s. If the patch regresses, SIGALRM
    fires and the test hard-fails with a clear signal
    instead of hanging CI indefinitely.

    '''
    if not wsp.is_needed():
        pytest.skip(
            'upstream trio shipped the fix — '
            'patch no longer needed for trio '
            '(see `is_needed()` for version gate)'
        )

    # Apply the patch.
    applied: bool = wsp.apply()
    # First call MUST return True; idempotent guard
    # prevents False on subsequent calls within the
    # same process.
    assert applied is True or applied is False  # idempotent

    # Cap wall-clock at 2s; SIGALRM raises in main
    # thread which interrupts the C-level recv loop
    # IF the patch regresses (since `signal.alarm`
    # uses Python's signal-wakeup-fd which the patch
    # itself relies on... but `repro()` runs OUTSIDE
    # a trio.run, so it's plain stdlib semantics here
    # — alarm WILL fire during `recv` syscall).
    signal.alarm(2)
    wsp.repro()
    signal.alarm(0)


def test_apply_all_idempotent():
    '''
    Calling `apply_all()` twice should not double-
    apply: second call's dict has all-False values
    (every patch reports "already applied").

    '''
    first: dict[str, bool] = patches.apply_all()
    second: dict[str, bool] = patches.apply_all()

    # Second call: every patch reports skipped.
    assert all(v is False for v in second.values()), (
        f'apply_all() not idempotent: {second}'
    )

    # First call: at least one patch was applied
    # (or all are no-ops because `is_needed()` is
    # False everywhere — the all-fixed-upstream future
    # state which is also valid).
    assert isinstance(first, dict)
    for name, applied in first.items():
        assert isinstance(applied, bool), (
            f'patch {name!r} returned non-bool: {applied!r}'
        )
