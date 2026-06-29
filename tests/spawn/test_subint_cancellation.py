'''
Cancellation + hard-kill semantics audit for the `subint` spawn
backend.

Exercises the escape-hatch machinery added to
`tractor.spawn._subint` (module-level `_HARD_KILL_TIMEOUT`,
bounded shields around the soft-kill / thread-join sites, daemon
driver-thread abandonment) so that future stdlib regressions or
our own refactors don't silently re-introduce the hangs first
diagnosed during the Phase B.2/B.3 bringup (issue #379).

Every test in this module:
- is wrapped in `trio.fail_after()` for a deterministic per-test
  wall-clock ceiling (the whole point of these tests is to fail
  fast when our escape hatches regress; an unbounded test would
  defeat itself),
- arms `tractor.devx.dump_on_hang()` to capture a stack dump on
  failure — without it, a hang here is opaque because pytest's
  stderr capture swallows `faulthandler` output by default
  (hard-won lesson from the original diagnosis),
- skips on py<3.13 (no `_interpreters`) and on any
  `--spawn-backend` other than `'subint'` (these tests are
  subint-specific by design — they'd be nonsense under `trio` or
  `mp_*`).

'''
from __future__ import annotations
from functools import partial

import pytest
import trio
import tractor
from tractor.devx import dump_on_hang


# Gate: the `subint` backend requires py3.14+. Check the
# public stdlib wrapper's presence (added in 3.14) rather than
# the private `_interpreters` module (which exists on 3.13 but
# wedges under tractor's usage — see `tractor.spawn._subint`).
pytest.importorskip('concurrent.interpreters')

# Subint-only: read the spawn method that `pytest_configure`
# committed via `try_set_start_method()`. By the time this module
# imports, the CLI backend choice has been applied.
from tractor.spawn._spawn import _spawn_method  # noqa: E402

if _spawn_method != 'subint':
    pytestmark = pytest.mark.skip(
        reason=(
            "subint-specific cancellation audit — "
            "pass `--spawn-backend=subint` to run."
        ),
    )


# ----------------------------------------------------------------
# child-side task bodies (run inside the spawned subint)
# ----------------------------------------------------------------


async def _trivial_rpc() -> str:
    '''
    Minimal RPC body for the baseline happy-teardown test.
    '''
    return 'hello from subint'


async def _spin_without_trio_checkpoints() -> None:
    '''
    Block the main task with NO trio-visible checkpoints so any
    `Portal.cancel_actor()` arriving over IPC has nothing to hand
    off to.

    `threading.Event.wait(timeout)` releases the GIL (so other
    threads — including trio's IO/RPC tasks — can progress) but
    does NOT insert a trio checkpoint, so the subactor's main
    task never notices cancellation.

    This is the exact "stuck subint" scenario the hard-kill
    shields exist to survive.
    '''
    import threading
    never_set = threading.Event()
    while not never_set.is_set():
        # 1s re-check granularity; low enough not to waste CPU,
        # high enough that even a pathologically slow
        # `_HARD_KILL_TIMEOUT` won't accidentally align with a
        # wake.
        never_set.wait(timeout=1.0)


# ----------------------------------------------------------------
# parent-side harnesses (driven inside `trio.run(...)`)
# ----------------------------------------------------------------


async def _happy_path(
    reg_addr: tuple[str, int|str],
    deadline: float,
) -> None:
    with trio.fail_after(deadline):
        async with (
            tractor.open_root_actor(
                registry_addrs=[reg_addr],
            ),
            tractor.open_nursery() as an,
        ):
            portal: tractor.Portal = await an.run_in_actor(
                _trivial_rpc,
                name='subint-happy',
            )
            result: str = await portal.wait_for_result()
            assert result == 'hello from subint'


async def _spawn_stuck_then_cancel(
    reg_addr: tuple[str, int|str],
    deadline: float,
) -> None:
    with trio.fail_after(deadline):
        async with (
            tractor.open_root_actor(
                registry_addrs=[reg_addr],
            ),
            tractor.open_nursery() as an,
        ):
            await an.run_in_actor(
                _spin_without_trio_checkpoints,
                name='subint-stuck',
            )
            # Give the child time to reach its non-checkpointing
            # loop before we cancel; the precise value doesn't
            # matter as long as it's a handful of trio schedule
            # ticks.
            await trio.sleep(0.5)
            an.cancel_scope.cancel()


# ----------------------------------------------------------------
# tests
# ----------------------------------------------------------------


def test_subint_happy_teardown(
    reg_addr: tuple[str, int|str],
) -> None:
    '''
    Baseline: spawn a subactor, do one portal RPC, close nursery
    cleanly. No cancel, no faults.

    If this regresses we know something's wrong at the
    spawn/teardown layer unrelated to the hard-kill escape
    hatches.

    '''
    deadline: float = 10.0
    with dump_on_hang(
        seconds=deadline,
        path='/tmp/subint_cancellation_happy.dump',
    ):
        trio.run(partial(_happy_path, reg_addr, deadline))


# Wall-clock bound via `pytest-timeout` (`method='thread'`)
# as defense-in-depth over the inner `trio.fail_after(15)`.
# Under the orphaned-channel hang class described in
# `ai/conc-anal/subint_cancel_delivery_hang_issue.md`, SIGINT
# is still deliverable and this test *should* be unwedgeable
# by the inner trio timeout — but sibling subint-backend
# tests in this repo have also exhibited the
# `subint_sigint_starvation_issue.md` GIL-starvation flavor,
# so `method='thread'` keeps us safe in case ordering or
# load shifts the failure mode.
@pytest.mark.timeout(
    3,  # NOTE never passes pre-3.14+ subints support.
    method='thread',
)
def test_subint_non_checkpointing_child(
    reg_addr: tuple[str, int|str],
) -> None:
    '''
    Cancel a subactor whose main task is stuck in a non-
    checkpointing Python loop.

    `Portal.cancel_actor()` may be delivered over IPC but the
    main task never checkpoints to observe the Cancelled —
    so the subint's `trio.run()` can't exit gracefully.

    The parent `subint_proc` bounded-shield + daemon-driver-
    thread combo should abandon the thread after
    `_HARD_KILL_TIMEOUT` and let the parent return cleanly.

    Wall-clock budget:
    - ~0.5s: settle time for child to enter the stuck loop
    - ~3s: `_HARD_KILL_TIMEOUT` (soft-kill wait)
    - ~3s: `_HARD_KILL_TIMEOUT` (thread-join wait)
    - margin

    KNOWN ISSUE (Ctrl-C-able hang):
    -------------------------------
    This test currently hangs past the hard-kill timeout for
    reasons unrelated to the subint teardown itself — after
    the subint is destroyed, a parent-side trio task appears
    to park on an orphaned IPC channel (no clean EOF
    delivered to a waiting receive). Unlike the
    SIGINT-starvation sibling case in
    `test_stale_entry_is_deleted`, this hang IS Ctrl-C-able
    (`strace` shows SIGINT wakeup-fd `write() = 1`, not
    `EAGAIN`) — i.e. the main trio loop is still iterating
    normally. That makes this *our* bug to fix, not a
    CPython-level limitation.

    See `ai/conc-anal/subint_cancel_delivery_hang_issue.md`
    for the full analysis + candidate fix directions
    (explicit parent-side channel abort in `subint_proc`
    teardown being the most likely surgical fix).

    The sibling `ai/conc-anal/subint_sigint_starvation_issue.md`
    documents the *other* hang class (abandoned-legacy-subint
    thread + shared-GIL starvation → signal-wakeup-fd pipe
    fills → SIGINT silently dropped) — that one is
    structurally blocked on msgspec PEP 684 adoption and is
    NOT what this test is hitting.

    '''
    deadline: float = 15.0
    with dump_on_hang(
        seconds=deadline,
        path='/tmp/subint_cancellation_stuck.dump',
    ):
        trio.run(
            partial(
                _spawn_stuck_then_cancel,
                reg_addr,
                deadline,
            ),
        )
