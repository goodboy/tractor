# `subint_forkserver` backend leaks subactor descendants in `test_cancellation.py`

Follow-up tracker: surfaced while wiring the new
`subint_forkserver` spawn backend into the full tractor
test matrix (step 2 of the post-backend-lands plan;
see also
`ai/conc-anal/subint_forkserver_orphan_sigint_hang_issue.md`).

## TL;DR

Running `tests/test_cancellation.py` under
`--spawn-backend=subint_forkserver` reproducibly leaks
**exactly 5 `subint-forkserv` comm-named child processes**
after the pytest session exits. Both previously-run
sessions produced the same 5-process signature — not a
flake. Each leaked process holds a `LISTEN` on the
default registry TCP addr (`127.0.0.1:1616`), which
poisons any subsequent tractor test session that
defaults to that addr.

## Stopgap (not the real fix)

Multiple tests in `test_cancellation.py` were calling
`tractor.open_nursery()` **without** passing
`registry_addrs=[reg_addr]`, i.e. falling back on the
default `:1616`. The commit accompanying this doc wires
the `reg_addr` fixture through those tests so each run
gets a session-unique port — leaked zombies can no
longer poison **other** tests (they hold their own
unique port instead).

Tests touched (in `tests/test_cancellation.py`):

- `test_cancel_infinite_streamer`
- `test_some_cancels_all`
- `test_nested_multierrors`
- `test_cancel_via_SIGINT`
- `test_cancel_via_SIGINT_other_task`

This is a **suite-hygiene fix** — it doesn't close the
actual leak; it just stops the leak from blast-radiusing.
Zombie descendants still accumulate per run.

## The real bug (unfixed)

`subint_forkserver_proc`'s teardown — `_ForkedProc.kill()`
(plain `os.kill(SIGKILL)` to the direct child pid) +
`proc.wait()` — does **not** reap grandchildren or
deeper descendants. When a cancellation test causes a
multi-level actor tree to tear down, the direct child
dies but its own children survive and get reparented to
init (PID 1), where they stay running with their
inherited FDs (including the registry listen socket).

**Symptom on repro:**

```
$ ss -tlnp 2>/dev/null | grep ':1616'
LISTEN 0 4096 127.0.0.1:1616 0.0.0.0:* \
    users:(("subint-forkserv",pid=211595,fd=17),
           ("subint-forkserv",pid=211585,fd=17),
           ("subint-forkserv",pid=211583,fd=17),
           ("subint-forkserv",pid=211576,fd=17),
           ("subint-forkserv",pid=211572,fd=17))

$ for p in 211572 211576 211583 211585 211595; do
    cat /proc/$p/cmdline | tr '\0' ' '; echo; done
./py314/bin/python -m pytest --spawn-backend=subint_forkserver \
  tests/test_cancellation.py --timeout=30 --timeout-method=signal \
  --tb=no -q --no-header
... (x5, all same cmdline — inherited from fork)
```

All 5 share the pytest cmdline because `os.fork()`
without `exec()` preserves the parent's argv. Their
comm-name (`subint-forkserv`) is the `thread_name` we
pass to the fork-worker thread in
`tractor.spawn._subint_forkserver.fork_from_worker_thread`.

## Why 5?

Not confirmed; guess is 5 = the parametrize cardinality
of one of the leaky tests (e.g. `test_some_cancels_all`
has 5 parametrize cases). Each param-case spawns a
nested tree; each leaks exactly one descendant. Worth
verifying by running each parametrize-case individually
and counting leaked procs per case.

## Ruled out

- **`:1616` collision from a different repo** (e.g.
  piker): `/proc/$pid/cmdline` + `cwd` both resolve to
  the tractor repo's `py314/` venv for all 5. These are
  definitively spawned by our test run.
- **Parent-side `_ForkedProc.wait()` regressed**: the
  direct child's teardown completes cleanly (exit-code
  captured, `waitpid` returns); the 5 survivors are
  deeper-descendants whose parent-side shim has no
  handle on them. So the bug isn't in
  `_ForkedProc.wait()` — it's in the lack of tree-
  level descendant enumeration + reaping during nursery
  teardown.

## Likely fix directions

1. **Process-group-scoped spawn + tree kill.** Put each
   forkserver-spawned subactor into its own process
   group (`os.setpgrp()` in the fork child), then on
   teardown `os.killpg(pgid, SIGKILL)` to reap the
   whole tree atomically. Simplest, most surgical.
2. **Subreaper registration.** Use
   `PR_SET_CHILD_SUBREAPER` on the tractor root so
   orphaned grandchildren reparent to the root rather
   than init — then we can `waitpid` them from the
   parent-side nursery teardown. More invasive.
3. **Explicit descendant enumeration at teardown.**
   In `subint_forkserver_proc`'s finally block, walk
   `/proc/<pid>/task/*/children` before issuing SIGKILL
   to build a descendant-pid set; then kill + reap all
   of them. Fragile (Linux-only, proc-fs-scan race).

Vote: **(1)** — clean, POSIX-standard, aligns with how
`subprocess.Popen` (and by extension `trio.lowlevel.
open_process`) handle tree-kill semantics on
kwargs-supplied `start_new_session=True`.

## Reproducer

```sh
# before: ensure clean env
ss -tlnp 2>/dev/null | grep ':1616' || echo 'clean'

# run the leaky tests
./py314/bin/python -m pytest \
  --spawn-backend=subint_forkserver \
  tests/test_cancellation.py \
  --timeout=30 --timeout-method=signal --tb=no -q --no-header

# observe: 5 leaked children now holding :1616
ss -tlnp 2>/dev/null | grep ':1616'
```

Expected output: `subint-forkserv` processes listed as
listeners on `:1616`. Cleanup:

```sh
pkill -9 -f \
  "$(pwd)/py314/bin/python.*pytest.*spawn-backend=subint_forkserver"
```

## References

- `tractor/spawn/_subint_forkserver.py::_ForkedProc`
  — the current teardown shim; PID-scoped, not tree-
  scoped.
- `tractor/spawn/_subint_forkserver.py::subint_forkserver_proc`
  — the spawn backend whose `finally` block needs the
  tree-kill fix.
- `tests/test_cancellation.py` — the surface where the
  leak surfaces.
- `ai/conc-anal/subint_forkserver_orphan_sigint_hang_issue.md`
  — sibling tracker for a different forkserver-teardown
  class (orphaned child doesn't respond to SIGINT); may
  share root cause with this one once the fix lands.
- tractor issue #379 — subint backend tracking.
