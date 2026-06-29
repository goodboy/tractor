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

## Snapshot evidence (2026-05-13)

After landing the `fail_after_w_trace` /
`afk_alarm_w_trace` capture-on-timeout helpers
(`tractor._testing.trace`), `test_nested_multierrors`
on the `main_thread_forkserver` backend produces
**reproducible diag snapshots** at
`$XDG_CACHE_HOME/tractor/hung-dumps/test_nested_multierrors_start_method_main_thread_forkserver__<iso-ts>/`.

### Reproduction

```bash
pytest \
  -v --verbose --durations=10 \
  --spawn-backend=main_thread_forkserver \
  --tpt-proto=uds \
  --capture=sys --show-capture=stderr -rxX \
  tests/test_cancellation.py::test_nested_multierrors
```

The test is `xfail(strict=False)` for MTF — it RUNS
each invocation so snapshots accumulate, but doesn't
break `--lf` workflow.

### Consistent shape across runs

5+ snapshots taken back-to-back show the SAME pattern:

- **Timing:** ~10s wall-clock total. Inner
  `fail_after_w_trace(10)` fires at exactly T=10s;
  cascade's `nursery.__aexit__` takes ~0.6s more to
  gather + propagate the resulting
  `BaseExceptionGroup`. **Trio backend completes the
  SAME test in <6s** — so the MTF cascade is ~2x
  slower at minimum.

- **`BaseExceptionGroup` shape:** mixed
  `[RemoteActorError, Cancelled]`. The first
  subactor's natural error-propagation (`assert 0`
  raised → `RemoteActorError` portal-result)
  arrives before T=10s; the OTHER subactor's
  portal-wait is still in flight at T=10s, gets
  cancelled by `fail_after_w_trace`'s scope-cancel
  → returns `Cancelled` instead.

- **Orphan-spawn skew:** snapshot's `orphans` bucket
  (after the `_is_tractor_subactor` cgroup-slice
  override fix) consistently shows 2-4 init-adopted
  procs at `depth_3` and `depth_1` levels — these
  are the leaves whose parent (`depth_2` spawner)
  was killed mid-cascade but who hadn't yet seen
  the cancel signal themselves.

- **UDS sock-leak:** 2-6 dead-orphan socks per run
  (varies with cascade timing). The
  `track_orphaned_uds_per_test` fixture reaps them
  post-test → contamination is isolated per-invocation.

### Capture mechanism

`fail_after_w_trace` covers two firing paths:

1. **`trio.TooSlowError`** raised at scope-exit
   (body returned cleanly past deadline) — direct
   `except` handler captures.

2. **Scope-cancel + body raises non-`Cancelled` exc**
   (e.g. `nursery.__aexit__` wraps timeout-induced
   `Cancelled` into a `BaseExceptionGroup` that
   escapes before `trio.fail_after`'s exit-check
   could fire `TooSlowError`) — body-raise `except`
   handler checks `scope.cancel_called` and
   captures if True. This path catches the
   `test_nested_multierrors` shape specifically (see
   "BaseExceptionGroup shape" above).

The snapshot dir contains:
- `trace.txt` — `ptree` + `hung_state` (kernel
  `wchan`/`stack` + `py-spy dump --locals` when
  sudo cached), with `include_strays=True`
  surfacing any cross-test ghost subactor trees in
  the `orphans` bucket.
- `bindspace.txt` — UDS bindspace classification
  (live-active / orphaned-alive / orphaned-dead).
- `meta.json` — `{pid, label, captured_at, sudo_cached}`.

The end-of-session `pytest_terminal_summary` hook
in `tractor._testing.pytest` lists every snapshot
dir from the run so you don't have to scroll back
through captured-stderr lines:

```
========================= tractor hang-snapshot index ==========================
N `fail_after_w_trace` / `afk_alarm_w_trace` snapshot(s) captured this session:
  <test-id>
    → /home/.../.cache/tractor/hung-dumps/<label>__<ts>
```

### Caveats

The snapshot fires AFTER the body-raise (not at the
exact moment of scope-cancel), so the parent's
py-spy frames show `_do_capture_snapshot` itself
running, NOT the cancel-cascade hang frame. To see
the actual hang state, manual `acli.ptree` /
`acli.hung_dump` from a second terminal at T=10s
would be needed — **not currently possible**
because per-test reaper fixtures clean up ~0.6s
post-timeout. See follow-up TODO in
`tractor/_testing/trace.py` for a
`TRACTOR_TRACE_HOLD=1` env-var pause mode.

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
