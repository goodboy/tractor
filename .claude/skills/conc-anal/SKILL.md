---
name: conc-anal
description: >
  Concurrency analysis for tractor's trio-based
  async primitives. Trace task scheduling across
  checkpoint boundaries, identify race windows in
  shared mutable state, and verify synchronization
  correctness. Invoke on code segments the user
  points at, OR proactively when reviewing/writing
  concurrent cache, lock, or multi-task acm code.
argument-hint: "[file:line-range or function name]"
allowed-tools:
  - Read
  - Grep
  - Glob
  - Task
---

Perform a structured concurrency analysis on the
target code. This skill should be invoked:

- **On demand**: user points at a code segment
  (file:lines, function name, or pastes a snippet)
- **Proactively**: when writing or reviewing code
  that touches shared mutable state across trio
  tasks — especially `_Cache`, locks, events, or
  multi-task `@acm` lifecycle management

## 0. Identify the target

If the user provides a file:line-range or function
name, read that code. If not explicitly provided,
identify the relevant concurrent code from context
(e.g. the current diff, a failing test, or the
function under discussion).

## 1. Inventory shared mutable state

List every piece of state that is accessed by
multiple tasks. For each, note:

- **What**: the variable/dict/attr (e.g.
  `_Cache.values`, `_Cache.resources`,
  `_Cache.users`)
- **Scope**: class-level, module-level, or
  closure-captured
- **Writers**: which tasks/code-paths mutate it
- **Readers**: which tasks/code-paths read it
- **Guarded by**: which lock/event/ordering
  protects it (or "UNGUARDED" if none)

Format as a table:

```
| State               | Writers         | Readers         | Guard          |
|---------------------|-----------------|-----------------|----------------|
| _Cache.values       | run_ctx, moc¹   | moc             | ctx_key lock   |
| _Cache.resources    | run_ctx, moc    | moc, run_ctx    | UNGUARDED      |
```

¹ `moc` = `maybe_open_context`

## 2. Map checkpoint boundaries

For each code path through the target, mark every
**checkpoint** — any `await` expression where trio
can switch to another task. Use line numbers:

```
L325: await lock.acquire()        ← CHECKPOINT
L395: await service_tn.start(...) ← CHECKPOINT
L411: lock.release()              ← (not a checkpoint, but changes lock state)
L414: yield (False, yielded)      ← SUSPEND (caller runs)
L485: no_more_users.set()         ← (wakes run_ctx, no switch yet)
```

**Key trio scheduling rules to apply:**
- `Event.set()` makes waiters *ready* but does NOT
  switch immediately
- `lock.release()` is not a checkpoint
- `await sleep(0)` IS a checkpoint
- Code in `finally` blocks CAN have checkpoints
  (unlike asyncio)
- `await` inside `except` blocks can be
  `trio.Cancelled`-masked

## 3. Trace concurrent task schedules

Write out the **interleaved execution trace** for
the problematic scenario. Number each step and tag
which task executes it:

```
[Task A]  1. acquires lock
[Task A]  2. cache miss → allocates resources
[Task A]  3. releases lock
[Task A]  4. yields to caller
[Task A]  5. caller exits → finally runs
[Task A]  6. users-- → 0, sets no_more_users
[Task A]  7. pops lock from _Cache.locks
[run_ctx] 8. wakes from no_more_users.wait()
[run_ctx] 9. values.pop(ctx_key)
[run_ctx] 10. acm __aexit__ → CHECKPOINT
[Task B]  11. creates NEW lock (old one popped)
[Task B]  12. acquires immediately
[Task B]  13. values[ctx_key] → KeyError
[Task B]  14. resources[ctx_key] → STILL EXISTS
[Task B]  15. 💥 RuntimeError
```

Identify the **race window**: the range of steps
where state is inconsistent. In the example above,
steps 9–10 are the window (values gone, resources
still alive).

## 4. Classify the bug

Categorize what kind of concurrency issue this is:

- **TOCTOU** (time-of-check-to-time-of-use): state
  changes between a check and the action based on it
- **Stale reference**: a task holds a reference to
  state that another task has invalidated
- **Lifetime mismatch**: a synchronization primitive
  (lock, event) has a shorter lifetime than the
  state it's supposed to protect
- **Missing guard**: shared state is accessed
  without any synchronization
- **Atomicity gap**: two operations that should be
  atomic have a checkpoint between them

## 5. Propose fixes

For each proposed fix, provide:

- **Sketch**: pseudocode or diff showing the change
- **How it closes the window**: which step(s) from
  the trace it eliminates or reorders
- **Tradeoffs**: complexity, perf, new edge cases,
  impact on other code paths
- **Risk**: what could go wrong (deadlocks, new
  races, cancellation issues)

Rate each fix: `[simple|moderate|complex]` impl
effort.

## 6. Output format

Structure the full analysis as:

```markdown
## Concurrency analysis: `<target>`

### Shared state
<table from step 1>

### Checkpoints
<list from step 2>

### Race trace
<interleaved trace from step 3>

### Classification
<bug type from step 4>

### Fixes
<proposals from step 5>
```

## Tractor-specific patterns to watch

These are known problem areas in tractor's
concurrency model. Flag them when encountered:

### `_Cache` lock vs `run_ctx` lifetime

The `_Cache.locks` entry is managed by
`maybe_open_context` callers, but `run_ctx` runs
in `service_tn` — a different task tree. Lock
pop/release in the caller's `finally` does NOT
wait for `run_ctx` to finish tearing down. Any
state that `run_ctx` cleans up in its `finally`
(e.g. `resources.pop()`) is vulnerable to
re-entry races after the lock is popped.

### `values.pop()` → acm `__aexit__` → `resources.pop()` gap

In `_Cache.run_ctx`, the inner `finally` pops
`values`, then the acm's `__aexit__` runs (which
has checkpoints), then the outer `finally` pops
`resources`. This creates a window where `values`
is gone but `resources` still exists — a classic
atomicity gap.

### Global vs per-key counters

`_Cache.users` as a single `int` (pre-fix) meant
that users of different `ctx_key`s inflated each
other's counts, preventing teardown when one key's
users hit zero. Always verify that per-key state
(`users`, `locks`) is actually keyed on `ctx_key`
and not on `fid` or some broader key.

### `Event.set()` wakes but doesn't switch

`trio.Event.set()` makes waiting tasks *ready* but
the current task continues executing until its next
checkpoint. Code between `.set()` and the next
`await` runs atomically from the scheduler's
perspective. Use this to your advantage (or watch
for bugs where code assumes the woken task runs
immediately).

### `except` block checkpoint masking

`await` expressions inside `except` handlers can
be masked by `trio.Cancelled`. If a `finally`
block runs from an `except` and contains
`lock.release()`, the release happens — but any
`await` after it in the same `except` may be
swallowed. This is why `maybe_open_context`'s
cache-miss path does `lock.release()` in a
`finally` inside the `except KeyError`.

### Cancellation in `finally`

Unlike asyncio, trio allows checkpoints in
`finally` blocks. This means `finally` cleanup
that does `await` can itself be cancelled (e.g.
by nursery shutdown). Watch for cleanup code that
assumes it will run to completion.

### Unbounded waits in cleanup paths

Any `await <event>.wait()` in a teardown path is
a latent deadlock unless the event's setter is
GUARANTEED to fire. If the setter depends on
external state (peer disconnects, child process
exit, subsequent task completion) that itself
depends on the current task's progress, you have
a mutual wait.

Rule: **bound every `await X.wait()` in cleanup
paths with `trio.move_on_after()`** unless you
can prove the setter is unconditionally reachable
from the state at the await site. Concrete recent
example: `ipc_server.wait_for_no_more_peers()` in
`async_main`'s finally (see
`ai/conc-anal/subint_forkserver_test_cancellation_leak_issue.md`
"probe iteration 3") — it was unbounded, and when
one peer-handler was stuck the wait-for-no-more-
peers event never fired, deadlocking the whole
actor-tree teardown cascade.

### The capture-pipe-fill hang pattern (grep this first)

When investigating any hang in the test suite
**especially under fork-based backends**, first
check whether the hang reproduces under `pytest
-s` (`--capture=no`). If `-s` makes it go away
you're not looking at a trio concurrency bug —
you're looking at a Linux pipe-buffer fill.

Mechanism: pytest replaces fds 1,2 with pipe
write-ends. Fork-child subactors inherit those
fds. High-volume error-log tracebacks (cancel
cascade spew) fill the 64KB pipe buffer. Child
`write()` blocks. Child can't exit. Parent's
`waitpid`/pidfd wait blocks. Deadlock cascades up
the tree.

Pre-existing guards in `tests/conftest.py` encode
this knowledge — grep these BEFORE blaming
concurrency:

```python
# tests/conftest.py:258
if loglevel in ('trace', 'debug'):
    # XXX: too much logging will lock up the subproc (smh)
    loglevel: str = 'info'

# tests/conftest.py:316
# can lock up on the `_io.BufferedReader` and hang..
stderr: str = proc.stderr.read().decode()
```

Full post-mortem +
`ai/conc-anal/subint_forkserver_test_cancellation_leak_issue.md`
for the canonical reproduction. Cost several
investigation sessions before catching it —
because the capture-pipe symptom was masked by
deeper cascade-deadlocks. Once the cascades were
fixed, the tree tore down enough to generate
pipe-filling log volume → capture-pipe finally
surfaced. Grep-note for future-self: **if a
multi-subproc tractor test hangs, `pytest -s`
first, conc-anal second.**
