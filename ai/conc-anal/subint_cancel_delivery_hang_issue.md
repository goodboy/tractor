# `subint` backend: parent trio loop parks after subint teardown (Ctrl-C works; not a CPython-level issue)

Follow-up to the Phase B subint spawn-backend PR (see
`tractor.spawn._subint`, issue #379). Distinct from the
`subint_sigint_starvation_issue.md` (SIGINT-unresponsive
starvation hang): this one is **Ctrl-C-able**, which means
it's *not* the shared-GIL-hostage class and is ours to fix
from inside tractor rather than waiting on upstream CPython
/ msgspec progress.

## TL;DR

After a stuck-subint subactor is torn down via the
hard-kill path, a parent-side trio task parks on an
*orphaned resource* (most likely a `chan.recv()` /
`process_messages` loop on the now-dead subint's IPC
channel) and waits forever for bytes that can't arrive —
because the channel was torn down without emitting a clean
EOF/`BrokenResourceError` to the waiting receiver.

Unlike `subint_sigint_starvation_issue.md`, the main trio
loop **is** iterating normally — SIGINT delivers cleanly
and the test unhangs. But absent Ctrl-C, the test suite
wedges indefinitely.

## Symptom

Running `test_subint_non_checkpointing_child` under
`--spawn-backend=subint` (in
`tests/test_subint_cancellation.py`):

1. Test spawns a subactor whose main task runs
   `threading.Event.wait(1.0)` in a loop — releases the
   GIL but never inserts a trio checkpoint.
2. Parent does `an.cancel_scope.cancel()`. Our
   `subint_proc` cancel path fires: soft-kill sends
   `Portal.cancel_actor()` over the live IPC channel →
   subint's trio loop *should* process the cancel msg on
   its IPC dispatcher task (since the GIL releases are
   happening).
3. Expected: subint's `trio.run()` unwinds, driver thread
   exits naturally, parent returns.
4. Actual: parent `trio.run()` never completes. Test
   hangs past its `trio.fail_after()` deadline.

## Evidence

### `strace` on the hung pytest process during SIGINT

```
--- SIGINT {si_signo=SIGINT, si_code=SI_KERNEL} ---
write(17, "\2", 1)                      = 1
```

Contrast with the SIGINT-starvation hang (see
`subint_sigint_starvation_issue.md`) where that same
`write()` returned `EAGAIN`. Here the SIGINT byte is
written successfully → Python's signal handler pipe is
being drained → main trio loop **is** iterating → SIGINT
gets turned into `trio.Cancelled` → the test unhangs (if
the operator happens to be there to hit Ctrl-C).

### Stack dump (via `tractor.devx.dump_on_hang`)

Single main thread visible, parked in
`trio._core._io_epoll.get_events` inside `trio.run` at the
test's `trio.run(...)` call site. No subint driver thread
(subint was destroyed successfully — this is *after* the
hard-kill path, not during it).

## Root cause hypothesis

Most consistent with the evidence: a parent-side trio
task is awaiting a `chan.recv()` / `process_messages` loop
on the dead subint's IPC channel. The sequence:

1. Soft-kill in `subint_proc` sends `Portal.cancel_actor()`
   over the channel. The subint's trio dispatcher *may* or
   may not have processed the cancel msg before the subint
   was destroyed — timing-dependent.
2. Hard-kill timeout fires (because the subint's main
   task was in `threading.Event.wait()` with no trio
   checkpoint — cancel-msg processing couldn't race the
   timeout).
3. Driver thread abandoned, `_interpreters.destroy()`
   runs. Subint is gone.
4. But the parent-side trio task holding a
   `chan.recv()` / `process_messages` loop against that
   channel was **not** explicitly cancelled. The channel's
   underlying socket got torn down, but without a clean
   EOF delivered to the waiting recv, the task parks
   forever on `trio.lowlevel.wait_readable` (or similar).

This matches the "main loop fine, task parked on
orphaned I/O" signature.

## Why this is ours to fix (not CPython's)

- Main trio loop iterates normally → GIL isn't starved.
- SIGINT is deliverable → not a signal-pipe-full /
  wakeup-fd contention scenario.
- The hang is in *our* supervision code, specifically in
  how `subint_proc` tears down its side of the IPC when
  the subint is abandoned/destroyed.

## Possible fix directions

1. **Explicit parent-side channel abort on subint
   abandon.** In `subint_proc`'s teardown block, after the
   hard-kill timeout fires, explicitly close the parent's
   end of the IPC channel to the subint. Any waiting
   `chan.recv()` / `process_messages` task sees
   `BrokenResourceError` (or `ClosedResourceError`) and
   unwinds.
2. **Cancel parent-side RPC tasks tied to the dead
   subint's channel.** The `Actor._rpc_tasks` / nursery
   machinery should have a handle on any
   `process_messages` loops bound to a specific peer
   channel. Iterate those and cancel explicitly.
3. **Bound the top-level `await actor_nursery
   ._join_procs.wait()` shield in `subint_proc`** (same
   pattern as the other bounded shields the hard-kill
   patch added). If the nursery never sets `_join_procs`
   because a child task is parked, the bound would at
   least let the teardown proceed.

Of these, (1) is the most surgical and directly addresses
the root cause. (2) is a defense-in-depth companion. (3)
is a band-aid but cheap to add.

## Current workaround

None in-tree. The test's `trio.fail_after()` bound
currently fires and raises `TooSlowError`, so the test
visibly **fails** rather than hangs — which is
intentional (an unbounded cancellation-audit test would
defeat itself). But in interactive test runs the operator
has to hit Ctrl-C to move past the parked state before
pytest reports the failure.

## Reproducer

```
./py314/bin/python -m pytest \
  tests/test_subint_cancellation.py::test_subint_non_checkpointing_child \
  --spawn-backend=subint --tb=short --no-header -v
```

Expected: hangs until `trio.fail_after(15)` fires, or
Ctrl-C unwedges it manually.

## References

- `tractor.spawn._subint.subint_proc` — current subint
  teardown code; see the `_HARD_KILL_TIMEOUT` bounded
  shields + `daemon=True` driver-thread abandonment
  (commit `b025c982`).
- `ai/conc-anal/subint_sigint_starvation_issue.md` — the
  sibling CPython-level hang (GIL-starvation,
  SIGINT-unresponsive) which is **not** this issue.
- Phase B tracking: issue #379.
