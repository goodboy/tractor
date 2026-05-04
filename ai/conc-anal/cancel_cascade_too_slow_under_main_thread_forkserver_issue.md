# Cancel-cascade `trio.TooSlowError` flakes under `main_thread_forkserver`

## Symptom

Running the full test suite under

```bash
./py313/bin/python -m pytest tests/ \
  --tpt-proto=tcp \
  --spawn-backend=main_thread_forkserver
```

surfaces a single, **rotating** `trio.TooSlowError`
failure each run. The failure isn't deterministic on
test identity — different test each run — but it
ALWAYS looks like:

```
FAILED tests/<file>::test_<name> - trio.TooSlowError
==== 1 failed, 373 passed, 17 skipped, 11–12 xfailed,
       0–1 xpassed, ~550 warnings in ~6min ====
```

Pass rate: **~99.7%** (373 of 374 non-skip tests).
Wall-clock per full run: 5–6 min.

## Tests observed flaking so far

Each row was the SOLE failure in a separate run:

| run # | test |
|---|---|
| 1 | `tests/test_advanced_streaming.py::test_dynamic_pub_sub[KeyboardInterrupt]` |
| 2 | `tests/test_infected_asyncio.py::test_context_spawns_aio_task_that_errors[parent_actor_cancels_child=False]` |

Both share the same shape:

- **Cancel cascade** of N subactors back to a parent root actor.
- N ≥ `multiprocessing.cpu_count()` for `test_dynamic_pub_sub`
  (it spawns `cpus - 1` consumers + publisher + dynamic-consumer).
- N ≈ 2 for `test_context_spawns_aio_task_that_errors` —
  but each subactor is `infect_asyncio=True`, so each
  cancel involves the trio↔asyncio guest-run unwind
  which is structurally heavier than pure-trio.
- Test wraps the cascade in `trio.fail_after(N seconds)`
  and the cap fires before the cascade completes.

The exact failing test rotates because each test is
independently close to the cap; whichever happens to
be unlucky in scheduling/CPU-contention on a given run
is the one that times out.

## Root-cause family

`hard_kill` (`tractor/spawn/_spawn.py:hard_kill`) runs
the SC-graceful teardown ladder per subactor:

1. `Portal.cancel_actor()` — graceful IPC cancel-req.
2. Wait `terminate_after=1.6s` for sub to exit.
3. If still alive: `proc.kill()` (SIGKILL).
4. (NEW) `_unlink_uds_bind_addrs()` — post-mortem
   sock-file cleanup for UDS leaks (issue #452 fix).

For a cascade of N subactors, each pays steps 1–4. If
graceful-cancel doesn't complete within 1.6s for ANY
sub, that sub eats a full 1.6s of `move_on_after` plus
the `proc.wait()` post-SIGKILL.

Worst case under fork backend with N=cpus subs:
- N × 1.6s = 16s+ on a 10-core box just for the
  graceful timeout phase
- Plus per-spawn fork-IPC handshake cost compounds
  during teardown (each sub's IPC cleanup goes through
  the same forkserver coordinator)
- Plus the new autouse fixtures
  (`_track_orphaned_uds_per_test`,
  `_detect_runaway_subactors_per_test`,
  `_reap_orphaned_subactors`) all run at test
  teardown, adding small (10s of ms) but cumulative
  overhead

Current cap: 30s (`fail_after_s = 30 if
is_forking_spawner else 12`). Empirically fits the
median run but the tail breaks ~0.3% of the time.

## NOT regressing

To confirm this is a flake and not a regression:

- Pre-`WakeupSocketpair`-patch baseline: tests
  HUNG INDEFINITELY (busy-loop never released).
- Post-patch: pass-or-fail-fast, ~99.7% pass, the
  occasional cap-hit fails in bounded time (<60s for
  the offending test).
- Same test PASSES under `--spawn-backend=trio`
  (no fork, no hard-kill compounding).

So the suite is dramatically better than before; the
remaining flake is a known-tolerable steady-state.

## Possible mitigations (ranked)

### A. Bump the cap further

Cheapest. Change the per-test `fail_after_s` from 30
to e.g. 60 for fork backends. Pros: trivial. Cons:
masks any genuine slowness regression we'd want to
catch.

### B. CPU-count-aware cap

For tests whose N scales with `cpu_count()`, scale
the cap too:

```python
fail_after_s = (
    max(30, cpu_count() * 3)  # 3s/actor floor
    if is_forking_spawner
    else 12
)
```

Pros: scales with the actual cancel-cascade work.
Cons: still arbitrary multiplier.

### C. `pytest-rerunfailures` for these tests only

Mark the known-flaky tests with
`@pytest.mark.flaky(reruns=1)` (needs
`pytest-rerunfailures` dep). Single retry hides
genuine ~0.3% transient flakes.

Pros: no cap change, surfaces persistent failures
loudly. Cons: adds a dep, retries can mask real bugs
if used widely.

### D. Reduce `hard_kill`'s `terminate_after`

Drop from 1.6s → 0.8s. Cuts the worst-case cascade
time roughly in half. Risks: fewer subs get a chance
to run their cleanup before SIGKILL → more orphaned
state for the autouse reapers to handle (ironically,
adds back overhead elsewhere).

### E. Profile + targeted fix

Add `log.devx()` markers in `hard_kill` to time each
phase. Identify if any subactor is consistently
hitting the 1.6s cap (vs. exiting in <0.1s). If so,
that sub has a teardown bug worth fixing at source.
Pros: actually fixes the underlying slowness. Cons:
real investigation work, deferred from this round.

## Recommendation

Land this issue-doc as the tracker. Apply **(B)** as
a small follow-up — cheap and proportional. If it
still flakes, escalate to **(E)** with a `log.devx()`
profile-pass.

`(C)` is a backstop if `(B)` doesn't quite get there
and we need green CI faster than (E) can deliver.

## Verification protocol

After applying any mitigation:

```bash
# Run the suite N times back-to-back, count failures.
# A persistent failure on the SAME test == real bug.
# Failures rotating across tests == still cap-related.

for i in $(seq 1 5); do
  ./py313/bin/python -m pytest tests/ \
    --tpt-proto=tcp \
    --spawn-backend=main_thread_forkserver \
    -q 2>&1 | tail -2
done
```

Target: 0 failures across 5 runs ⇒ ship. 1–2 failures
still rotating ⇒ apply (C). Same test failing twice
⇒ escalate to (E).

## See also

- [#452](https://github.com/goodboy/tractor/issues/452) —
  UDS sock-file leak (related — `hard_kill`'s
  cleanup phase contributes to cascade time)
- `ai/conc-anal/trio_wakeup_socketpair_busy_loop_under_fork_issue.md`
  — the upstream-trio fix that turned this from a
  100% hang into a 0.3% flake
- `ai/conc-anal/infected_asyncio_under_main_thread_forkserver_hang_issue.md`
  — the asyncio variant which contributes to one of
  the rotating failures
- `tractor/spawn/_spawn.py::hard_kill` — the SIGKILL
  cascade source
- `tractor/_testing/_reap.py::_track_orphaned_uds_per_test`,
  `_detect_runaway_subactors_per_test`,
  `_reap_orphaned_subactors` — autouse cleanup
  fixtures whose cumulative teardown overhead
  contributes to the cascade time
