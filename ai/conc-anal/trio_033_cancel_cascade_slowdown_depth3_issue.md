# `trio` 0.29 -> 0.33 slows the depth=3 cancel-cascade

## Symptom

After locking to `trio==0.33.0` (commit `c7741bba`, was
`0.29.0`), this test reliably trips its `fail_after`
deadline on the **`trio`** backend:

```
FAILED tests/test_cancellation.py::test_nested_multierrors[start_method=trio-depth=3]
  - AssertionError: assert False
    where False = isinstance(
      Cancelled(source='deadline', source_task=None, reason=None),
      tractor.RemoteActorError,
    )
```

A `fail_after_w_trace` hang-snapshot is captured for the
test each run (deadline-injected `Cancelled` wrapped into
the actor-nursery `BaseExceptionGroup`).

## Root cause (immediate)

The test budgets `fail_after(6)` for the `trio` backend.
That 6s was chosen (commit `32955db0`, while `trio==0.29`)
with the assertion that trio finishes "well under" 6s.
The `trio` 0.29 -> 0.33 bump slowed the depth=3 cascade
past that budget, so the 6s deadline now fires mid-cascade.

trio 0.33 added **cancel-reason tracking** — every
`Cancelled` now carries `(source=, reason=, source_task=)`.
The injected exc is `Cancelled(source='deadline')`, i.e.
trio itself naming our `fail_after(6)` scope as the cancel
origin. When that `Cancelled` collapses one branch of the
nursery BEG, the test's `isinstance(subexc,
RemoteActorError)` assertion fails. The healthy outcome is
`BEG = [RemoteActorError, RemoteActorError]`; the
`Cancelled` is purely an artifact of the deadline cutting
the cascade short.

## Measurements (standalone, this machine)

```
depth=1  trio   ~3.15s   PASS  (keeps 6s budget)
depth=3  trio   ~6.8-8.2s  FAIL @ 6s  (now bumped to 12s)
```

depth=1 still fits comfortably; only depth=3 (deeper
recursive spawn-and-error tree => more actors to reap)
exceeds the old budget. The ~2s/depth-level cost looks
like serialized per-actor reap / `terminate_after` waits.

## Mitigation applied

`test_nested_multierrors` now splits the `trio` budget:

```python
case ('trio', 1):
    timeout = 6
case ('trio', 3):
    timeout = 12   # was 6; see this doc
```

This stops the deadline from firing so the cascade
completes naturally to `[RAE, RAE]`.

## Also affected — same root cause, different test

`test_echoserver_detailed_mechanics[trio-raise_error=KeyboardInterrupt]`
(`tests/test_infected_asyncio.py`) tripped the *same*
slowdown via its much tighter `trio` budget of `1s`. The
single-aio-subactor teardown now takes ~1s, so the `1s`
`fail_after` raced the deadline (PASS at 0.99s / FAIL at
1.03s across back-to-back standalone runs). On a deadline-
fire the injected `Cancelled(source='deadline')` wraps the
mid-stream `KeyboardInterrupt` into a `BaseExceptionGroup`,
which is NOT a `KeyboardInterrupt` so the bare
`pytest.raises(KeyboardInterrupt)` fails. (The sibling
`raise_error=Exception` variant only "passes" by accident:
an `ExceptionGroup` *is-a* `Exception`, so its
`pytest.raises(Exception)` still matches even when wrapped.)

Mitigation: bump that `trio` budget `1 -> 4s` (matching the
forking-spawner case). Without a deadline-fire the KBI
propagates bare and the assertion passes.

## Open follow-up (the actual regression)

The budget bump is a band-aid — the underlying question is
**why** the depth=3 `trio` cancel-cascade went from <6s to
~7-8s across `trio` 0.29 -> 0.33. Candidate avenues:

- which scope owns the per-actor `terminate_after` wait,
  and are the tree's reaps concurrent or serialized?
- did trio 0.33's abort/reschedule or cancel-reason
  bookkeeping change checkpoint timing on the cancel path?

If/when the cascade speeds back up under-budget, depth=3
will start completing well under 12s — at which point the
budget can be tightened back toward 6s as a regression
tripwire. Related (different backend, same cascade class):
`cancel_cascade_too_slow_under_main_thread_forkserver_issue.md`.
