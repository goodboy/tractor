# `subint_forkserver` backend: `test_cancellation.py` multi-level cancel cascade hang

Follow-up tracker: surfaced while wiring the new
`subint_forkserver` spawn backend into the full tractor
test matrix (step 2 of the post-backend-lands plan).
See also
`ai/conc-anal/subint_forkserver_orphan_sigint_hang_issue.md`
— sibling tracker for a different forkserver-teardown
class which probably shares the same fundamental root
cause (fork-FD-inheritance across nested spawns).

## TL;DR

`tests/test_cancellation.py::test_nested_multierrors[subint_forkserver]`
hangs indefinitely under our new backend. The hang is
**inside the graceful IPC cancel cascade** — every actor
in the multi-level tree parks in `epoll_wait` waiting
for IPC messages that never arrive. Not a hard-kill /
tree-reap issue (we don't reach the hard-kill fallback
path at all).

Working hypothesis (unverified): **`os.fork()` from a
subactor inherits the root parent's IPC listener socket
FDs**. When a first-level subactor forkserver-spawns a
grandchild, that grandchild inherits both its direct
spawner's FDs AND the root's FDs — IPC message routing
becomes ambiguous (or silently sends to the wrong
channel), so the cancel cascade can't reach its target.

## Corrected diagnosis vs. earlier draft

An earlier version of this doc claimed the root cause
was **"forkserver teardown doesn't tree-kill
descendants"** (SIGKILL only reaches the direct child,
grandchildren survive and hold TCP `:1616`). That
diagnosis was **wrong**, caused by conflating two
observations:

1. *5-zombie leak holding :1616* — happened in my own
   workflow when I aborted a bg pytest task with
   `pkill` (SIGTERM/SIGKILL, not SIGINT). The abrupt
   kill skipped the graceful `ActorNursery.__aexit__`
   cancel cascade entirely, orphaning descendants to
   init. **This was my cleanup bug, not a forkserver
   teardown bug.** Codified the fix (SIGINT-first +
   bounded wait before SIGKILL) in
   `feedback_sc_graceful_cancel_first.md` +
   `.claude/skills/run-tests/SKILL.md`.
2. *`test_nested_multierrors` hangs indefinitely* —
   the real, separate, forkserver-specific bug
   captured by this doc.

The two symptoms are unrelated. The tree-kill / setpgrp
fix direction proposed earlier would not help (1) (SC-
graceful-cleanup is the right answer there) and would
not help (2) (the hang is in the cancel cascade, not
in the hard-kill fallback).

## Symptom

Reproducer (py3.14, clean env):

```sh
# preflight: ensure clean env
ss -tlnp 2>/dev/null | grep ':1616' && echo 'FOUL — cleanup first!' || echo 'clean'

./py314/bin/python -m pytest --spawn-backend=subint_forkserver \
  'tests/test_cancellation.py::test_nested_multierrors[subint_forkserver]' \
  --timeout=30 --timeout-method=thread --tb=short -v
```

Expected: `pytest-timeout` fires at 30s with a thread-
dump banner, but the process itself **remains alive
after timeout** and doesn't unwedge on subsequent
SIGINT. Requires SIGKILL to reap.

## Evidence (tree structure at hang point)

All 5 processes are kernel-level `S` (sleeping) in
`do_epoll_wait` (trio's event loop waiting on I/O):

```
PID     PPID    THREADS  NAME             ROLE
333986  1       2        subint-forkserv  pytest main (the test body)
333993  333986  3        subint-forkserv  "child 1" spawner subactor
  334003 333993 1        subint-forkserv  grandchild errorer under child-1
  334014 333993 1        subint-forkserv  grandchild errorer under child-1
333999  333986  1        subint-forkserv  "child 2" spawner subactor (NO grandchildren!)
```

### Asymmetric tree depth

The test's `spawn_and_error(breadth=2, depth=3)` should
have BOTH direct children spawning 2 grandchildren
each, going 3 levels deep. Reality:

- Child 1 (333993, 3 threads) DID spawn its two
  grandchildren as expected — fully booted trio
  runtime.
- Child 2 (333999, 1 thread) did NOT spawn any
  grandchildren — clearly never completed its
  nursery's first `run_in_actor`. Its 1-thread state
  suggests the runtime never fully booted (no trio
  worker threads for `waitpid`/IPC).

This asymmetry is the key clue: the two direct
children started identically but diverged. Probably a
race around fork-inherited state (listener FDs,
subactor-nursery channel state) that happens to land
differently depending on spawn ordering.

### Parent-side state

Thread-dump of pytest main (333986) at the hang:

- Main trio thread — parked in
  `trio._core._io_epoll.get_events` (epoll_wait on
  its event loop). Waiting for IPC from children.
- Two trio-cache worker threads — each parked in
  `outcome.capture(sync_fn)` calling
  `os.waitpid(child_pid, 0)`. These are our
  `_ForkedProc.wait()` off-loads. They're waiting for
  the direct children to exit — but children are
  stuck in their own epoll_wait waiting for IPC from
  the parent.

**It's a deadlock, not a leak:** the parent is
correctly running `soft_kill(proc, _ForkedProc.wait,
portal)` (graceful IPC cancel via
`Portal.cancel_actor()`), but the children never
acknowledge the cancel message (or the message never
reaches them through the tangled post-fork IPC).

## What's NOT the cause (ruled out)

- **`_ForkedProc.kill()` only SIGKILLs direct pid /
  missing tree-kill**: doesn't apply — we never reach
  the hard-kill path. The deadlock is in the graceful
  cancel cascade.
- **Port `:1616` contention**: ruled out after the
  `reg_addr` fixture-wiring fix; each test session
  gets a unique port now.
- **GIL starvation / SIGINT pipe filling** (class-A,
  `subint_sigint_starvation_issue.md`): doesn't apply
  — each subactor is its own OS process with its own
  GIL (not legacy-config subint).
- **Child-side `_trio_main` absorbing KBI**: grep
  confirmed; `_trio_main` only catches KBI at the
  `trio.run()` callsite, which is reached only if the
  trio loop exits normally. The children here never
  exit trio.run() — they're wedged inside.

## Hypothesis: FD inheritance across nested forks

`subint_forkserver_proc` calls
`fork_from_worker_thread()` which ultimately does
`os.fork()` from a dedicated worker thread. Standard
Linux/POSIX fork semantics: **the child inherits ALL
open FDs from the parent**, including listener
sockets, epoll fds, trio wakeup pipes, and the
parent's IPC channel sockets.

At root-actor fork-spawn time, the root's IPC server
listener FDs are open in the parent. Those get
inherited by child 1. Child 1 then forkserver-spawns
its OWN subactor (grandchild). The grandchild
inherits FDs from child 1 — but child 1's address
space still contains **the root's IPC listener FDs
too** (inherited at first fork). So the grandchild
has THREE sets of FDs:

1. Its own (created after becoming a subactor).
2. Its direct parent child-1's.
3. The ROOT's (grandparent's) — inherited transitively.

IPC message routing may be ambiguous in this tangled
state. Or a listener socket that the root thinks it
owns is actually open in multiple processes, and
messages sent to it go to an arbitrary one. That
would exactly match the observed "graceful cancel
never propagates".

This hypothesis predicts the bug **scales with fork
depth**: single-level forkserver spawn
(`test_subint_forkserver_spawn_basic`) works
perfectly, but any test that spawns a second level
deadlocks. Matches observations so far.

## Fix directions (to validate)

### 1. `close_fds=True` equivalent in `fork_from_worker_thread()`

`subprocess.Popen` / `trio.lowlevel.open_process` have
`close_fds=True` by default on POSIX — they
enumerate open FDs in the child post-fork and close
everything except stdio + any explicitly-passed FDs.
Our raw `os.fork()` doesn't. Adding the equivalent to
our `_worker` prelude would isolate each fork
generation's FD set.

Implementation sketch in
`tractor.spawn._subint_forkserver.fork_from_worker_thread._worker`:

```python
def _worker() -> None:
    pid: int = os.fork()
    if pid == 0:
        # CHILD: close inherited FDs except stdio + the
        # pid-pipe we just opened.
        keep: set[int] = {0, 1, 2, rfd, wfd}
        import resource
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        os.closerange(3, soft)  # blunt; or enumerate /proc/self/fd
        # ... then child_target() as before
```

Problem: overly aggressive — closes FDs the
grandchild might legitimately need (e.g. its parent's
IPC channel for the spawn-spec handshake, if we rely
on that). Needs thought about which FDs are
"inheritable and safe" vs. "inherited by accident".

### 2. Cloexec on tractor's own FDs

Set `FD_CLOEXEC` on tractor-created sockets (listener
sockets, IPC channel sockets, pipes). This flag
causes automatic close on `execve`, but since we
`fork()` without `exec()`, this alone doesn't help.
BUT — combined with a child-side explicit close-
non-cloexec loop, it gives us a way to mark "my
private FDs" vs. "safe to inherit". Most robust, but
requires tractor-wide audit.

### 3. Explicit FD cleanup in `_ForkedProc`/`_child_target`

Have `subint_forkserver_proc`'s `_child_target`
closure explicitly close the parent-side IPC listener
FDs before calling `_actor_child_main`. Requires
being able to enumerate "the parent's listener FDs
that the child shouldn't keep" — plausible via
`Actor.ipc_server`'s socket objects.

### 4. Use `os.posix_spawn` with explicit `file_actions`

Instead of raw `os.fork()`, use `os.posix_spawn()`
which supports explicit file-action specifications
(close this FD, dup2 that FD). Cleaner semantics, but
probably incompatible with our "no exec" requirement
(subint_forkserver is a fork-without-exec design).

**Likely correct answer: (3) — targeted FD cleanup
via `actor.ipc_server` handle.** (1) is too blunt,
(2) is too wide-ranging, (4) changes the spawn
mechanism.

## Reproducer (standalone, no pytest)

```python
# save as /tmp/forkserver_nested_hang_repro.py  (py3.14+)
import trio, tractor

async def assert_err():
    assert 0

async def spawn_and_error(breadth: int = 2, depth: int = 1):
    async with tractor.open_nursery() as n:
        for i in range(breadth):
            if depth > 0:
                await n.run_in_actor(
                    spawn_and_error,
                    breadth=breadth,
                    depth=depth - 1,
                    name=f'spawner_{i}_{depth}',
                )
            else:
                await n.run_in_actor(
                    assert_err,
                    name=f'errorer_{i}',
                )

async def _main():
    async with tractor.open_nursery() as n:
        for i in range(2):
            await n.run_in_actor(
                spawn_and_error,
                name=f'top_{i}',
                breadth=2,
                depth=1,
            )

if __name__ == '__main__':
    from tractor.spawn._spawn import try_set_start_method
    try_set_start_method('subint_forkserver')
    with trio.fail_after(20):
        trio.run(_main)
```

Expected (current): hangs on `trio.fail_after(20)`
— children never ack the error-propagation cancel
cascade. Pattern: top 2 direct children, 4
grandchildren, 1 errorer deadlocks while trying to
unwind through its parent chain.

After fix: `trio.TooSlowError`-free completion; the
root's `open_nursery` receives the
`BaseExceptionGroup` containing the `AssertionError`
from the errorer and unwinds cleanly.

## Update — 2026-04-23: partial fix landed, deeper layer surfaced

Three improvements landed as separate commits in the
`subint_forkserver_backend` branch (see `git log`):

1. **`_close_inherited_fds()` in fork-child prelude**
   (`tractor/spawn/_subint_forkserver.py`). POSIX
   close-fds-equivalent enumeration via
   `/proc/self/fd` (or `RLIMIT_NOFILE` fallback), keep
   only stdio. This is fix-direction (1) from the list
   above — went with the blunt form rather than the
   targeted enum-via-`actor.ipc_server` form, turns
   out the aggressive close is safe because every
   inheritable resource the fresh child needs
   (IPC-channel socket, etc.) is opened AFTER the
   fork anyway.
2. **`_ForkedProc.wait()` via `os.pidfd_open()` +
   `trio.lowlevel.wait_readable()`** — matches the
   `trio.Process.wait` / `mp.Process.sentinel` pattern
   used by `trio_proc` and `proc_waiter`. Gives us
   fully trio-cancellable child-wait (prior impl
   blocked a cache thread on a sync `os.waitpid` that
   was NOT trio-cancellable due to
   `abandon_on_cancel=False`).
3. **`_parent_chan_cs` wiring** in
   `tractor/runtime/_runtime.py`: capture the shielded
   `loop_cs` for the parent-channel `process_messages`
   task in `async_main`; explicitly cancel it in
   `Actor.cancel()` teardown. This breaks the shield
   during teardown so the parent-chan loop exits when
   cancel is issued, instead of parking on a parent-
   socket EOF that might never arrive under fork
   semantics.

**Concrete wins from (1):** the sibling
`subint_forkserver_orphan_sigint_hang_issue.md` class
is **now fixed** — `test_orphaned_subactor_sigint_cleanup_DRAFT`
went from strict-xfail to pass. The xfail mark was
removed; the test remains as a regression guard.

**test_nested_multierrors STILL hangs** though.

### Updated diagnosis (narrowed)

DIAGDEBUG instrumentation of `process_messages` ENTER/
EXIT pairs + `_parent_chan_cs.cancel()` call sites
showed (captured during a 20s-timeout repro):

- 80 `process_messages` ENTERs, 75 EXITs → 5 stuck.
- **All 40 `shield=True` ENTERs matched EXIT** — every
  shielded parent-chan loop exits cleanly. The
  `_parent_chan_cs` wiring works as intended.
- **The 5 stuck loops are all `shield=False`** — peer-
  channel handlers (inbound connections handled by
  `handle_stream_from_peer` in stream_handler_tn).
- After our `_parent_chan_cs.cancel()` fires, NEW
  shielded process_messages loops start (on the
  session reg_addr port — probably discovery-layer
  reconnection attempts). These don't block teardown
  (they all exit) but indicate the cancel cascade has
  more moving parts than expected.

### Remaining unknown

Why don't the 5 peer-channel loops exit when
`service_tn.cancel_scope.cancel()` fires? They're in
`stream_handler_tn` which IS `service_tn` in the
current configuration (`open_ipc_server(parent_tn=
service_tn, stream_handler_tn=service_tn)`). A
standard nursery-scope-cancel should propagate through
them — no shield, no special handler. Something
specific to the fork-spawned configuration keeps them
alive.

Candidate follow-up experiments:

- Dump the trio task tree at the hang point (via
  `stackscope` or direct trio introspection) to see
  what each stuck loop is awaiting. `chan.__anext__`
  on a socket recv? An inner lock? A shielded sub-task?
- Compare peer-channel handler lifecycle under
  `trio_proc` vs `subint_forkserver` with equivalent
  logging to spot the divergence.
- Investigate whether the peer handler is caught in
  the `except trio.Cancelled:` path at
  `tractor/ipc/_server.py:448` that re-raises — but
  re-raise means it should still exit. Unless
  something higher up swallows it.

### Attempted fix (DID NOT work) — hypothesis (3)

Tried: in `_serve_ipc_eps` finally, after closing
listeners, also iterate `server._peers` and
sync-close each peer channel's underlying stream
socket fd:

```python
for _uid, _chans in list(server._peers.items()):
    for _chan in _chans:
        try:
            _stream = _chan._transport.stream if _chan._transport else None
            if _stream is not None:
                _stream.socket.close()  # sync fd close
        except (AttributeError, OSError):
            pass
```

Theory: closing the socket fd from outside the stuck
recv task would make the recv see EBADF /
ClosedResourceError and unblock.

Result: `test_nested_multierrors[subint_forkserver]`
still hangs identically. Either:
- The sync `socket.close()` doesn't propagate into
  trio's in-flight `recv_some()` the way I expected
  (trio may hold an internal reference that keeps the
  fd open even after an external close), or
- The stuck recv isn't even the root blocker and the
  peer handlers never reach the finally for some
  reason I haven't understood yet.

Either way, the sync-close hypothesis is **ruled
out**. Reverted the experiment, restored the skip-
mark on the test.

### Aside: `-s` flag does NOT change `test_nested_multierrors` behavior

Tested explicitly: both with and without `-s`, the
test hangs identically. So the capture-pipe-fill
hypothesis is **ruled out** for this test.

The earlier `test_context_stream_semantics.py` `-s`
observation was most likely caused by a competing
pytest run in my session (confirmed via process list
— my leftover pytest was alive at that time and
could have been holding state on the default
registry port).

## Update — 2026-04-23 (late): cancel delivery ruled in, nursery-wait ruled BLOCKER

**New diagnostic run** instrumented
`handle_stream_from_peer` at ENTER / `except
trio.Cancelled:` / finally, plus `Actor.cancel()`
just before `self._parent_chan_cs.cancel()`. Result:

- **40 `handle_stream_from_peer` ENTERs**.
- **0 `except trio.Cancelled:` hits** — cancel
  never fires on any peer-handler.
- **35 finally hits** — those handlers exit via
  peer-initiated EOF (normal return), NOT cancel.
- **5 handlers never reach finally** — stuck forever.
- **`Actor.cancel()` fired in 12 PIDs** — but the
  PIDs with peer handlers that DIDN'T fire
  Actor.cancel are exactly **root + 2 direct
  spawners**. These 3 actors have peer handlers
  (for their own subactors) that stay stuck because
  **`Actor.cancel()` at these levels never runs**.

### The actual deadlock shape

`Actor.cancel()` lives in
`open_root_actor.__aexit__` / `async_main` teardown.
That only runs when the enclosing `async with
tractor.open_nursery()` exits. The nursery's
`__aexit__` calls the backend `*_proc` spawn target's
teardown, which does `soft_kill() →
_ForkedProc.wait()` on its child PID. That wait is
trio-cancellable via pidfd now (good) — but nothing
CANCELS it because the outer scope only cancels when
`Actor.cancel()` runs, which only runs when the
nursery completes, which waits on the child.

It's a **multi-level mutual wait**:

```
root              blocks on spawner.wait()
  spawner         blocks on grandchild.wait()
    grandchild    blocks on errorer.wait()
      errorer     Actor.cancel() ran, but process
                  may not have fully exited yet
                  (something in root_tn holding on?)
```

Each level waits for the level below. The bottom
level (errorer) reaches Actor.cancel(), but its
process may not fully exit — meaning its pidfd
doesn't go readable, meaning the grandchild's
waitpid doesn't return, meaning the grandchild's
nursery doesn't unwind, etc. all the way up.

### Refined question

**Why does an errorer process not exit after its
`Actor.cancel()` completes?**

Possibilities:
1. `_parent_chan_cs.cancel()` fires (shielded
   parent-chan loop unshielded), but the task is
   stuck INSIDE the shielded loop's recv in a way
   that cancel still can't break.
2. After `Actor.cancel()` returns, `async_main`
   still has other tasks in `root_tn` waiting for
   something that never arrives (e.g. outbound
   IPC reply delivery).
3. The `os._exit(rc)` in `_worker` (at
   `_subint_forkserver.py`) doesn't run because
   `_child_target` never returns.

Next-session candidate probes (in priority order):

1. **Instrument `_worker`'s fork-child branch** to
   confirm whether `child_target()` returns (and
   thus `os._exit(rc)` is reached) for errorer
   PIDs. If yes → process should die; if no →
   trace back into `_actor_child_main` /
   `_trio_main` / `async_main` to find the stuck
   spot.
2. **Instrument `async_main`'s final unwind** to
   see which await in the teardown doesn't
   complete.
3. **Compare under `trio_proc` backend** at the
   same `_worker`-equivalent level to see where
   the flows diverge.

### Rule-out: NOT a stuck peer-chan recv

Earlier hypothesis was that the 5 stuck peer-chan
loops were blocked on a socket recv that cancel
couldn't interrupt. This pass revealed the real
cause: cancel **never reaches those tasks** because
their owning actor's `Actor.cancel()` never runs.
The recvs are fine — they're just parked because
nothing is telling them to stop.

## Update — 2026-04-23 (very late): leaves exit, middle actors stuck in `trio.run`

Yet another instrumentation pass — this time
printing at:

- `_worker` child branch: `pre child_target()` /
  `child_target RETURNED rc=N` / `about to
  os._exit(rc)` 
- `_trio_main`: `about to trio.run` /
  `trio.run RETURNED NORMALLY` / `FINALLY`

**Fresh-run results** (`test_nested_multierrors[
subint_forkserver]`, depth=1/breadth=2, 1 root + 14
forked = 15 actors total):

- **9 processes completed the full flow** —
  `trio.run RETURNED NORMALLY` → `child_target
  RETURNED rc=0` → `about to os._exit(0)`. These
  are the LEAVES of the tree (errorer actors) plus
  their direct parents (depth-0 spawners). They
  actually exit their processes.
- **5 processes are stuck INSIDE `trio.run(trio_main)`**
  — they hit "about to trio.run" but NEVER see
  "trio.run RETURNED NORMALLY". These are root +
  top-level spawners + one intermediate.

**What this means:** `async_main` itself is the
deadlock holder, not the peer-channel loops.
Specifically, the outer `async with root_tn:` in
`async_main` never exits for the 5 stuck actors.
Their `trio.run` never returns → `_trio_main`
catch/finally never runs → `_worker` never reaches
`os._exit(rc)` → the PROCESS never dies → its
parent's `_ForkedProc.wait()` blocks → parent's
nursery hangs → parent's `async_main` hangs → ...

### The new precise question

**What task in the 5 stuck actors' `async_main`
never completes?** Candidates:

1. The shielded parent-chan `process_messages`
   task in `root_tn` — but we explicitly cancel it
   via `_parent_chan_cs.cancel()` in `Actor.cancel()`.
   However, `Actor.cancel()` only runs during
   `open_root_actor.__aexit__`, which itself runs
   only after `async_main`'s outer unwind — which
   doesn't happen. So the shield isn't broken.

2. `await actor_nursery._join_procs.wait()` or
   similar in the inline backend `*_proc` flow.

3. `_ForkedProc.wait()` on a grandchild that
   actually DID exit — but the pidfd_open watch
   didn't fire for some reason (race between
   pidfd_open and the child exiting?).

The most specific next probe: **add DIAG around
`_ForkedProc.wait()` enter/exit** to see whether
the pidfd-based wait returns for every grandchild
exit. If a stuck parent's `_ForkedProc.wait()`
NEVER returns despite its child exiting, the
pidfd mechanism has a race bug under nested
forkserver.

Alternative probe: instrument `async_main`'s outer
nursery exits to find which nursery's `__aexit__`
is stuck, drilling down from `trio.run` to the
specific `async with` that never completes.

### Cascade summary (updated tree view)

```
ROOT (pytest)                       STUCK in trio.run
├── top_0 (spawner, d=1)            STUCK in trio.run
│   ├── spawner_0_d1_0 (d=0)        exited (os._exit 0)
│   │   ├── errorer_0_0             exited (os._exit 0)
│   │   └── errorer_0_1             exited (os._exit 0)
│   └── spawner_0_d1_1 (d=0)        exited (os._exit 0)
│       ├── errorer_0_2             exited (os._exit 0)
│       └── errorer_0_3             exited (os._exit 0)
└── top_1 (spawner, d=1)            STUCK in trio.run
    ├── spawner_1_d1_0 (d=0)        STUCK in trio.run (sibling race?)
    │   ├── errorer_1_0             exited
    │   └── errorer_1_1             exited
    └── spawner_1_d1_1 (d=0)        STUCK in trio.run
        ├── errorer_1_2             exited
        └── errorer_1_3             exited
```

Grandchildren (d=0 spawners) exit OR stick —
asymmetric. Not purely depth-determined. Some race
condition in nursery teardown when multiple
siblings error simultaneously.

## Update — 2026-04-23 (late, probe iteration 3): hang pinpointed to `wait_for_no_more_peers()`

Further DIAGDEBUG at every milestone in `async_main`
(runtime UP / EXITED service_tn / EXITED root_tn /
FINALLY ENTER / RETURNING) plus `_ForkedProc.wait`
ENTER/RETURNED per-pidfd. Result:

**Every stuck actor reaches `async_main: FINALLY
ENTER` but NOT `async_main: RETURNING`.**

That isolates the hang to a specific await in
`async_main`'s finally block at
`tractor/runtime/_runtime.py:1837+`. The suspect:

```python
# Ensure all peers (actors connected to us as clients) are finished
if ipc_server := actor.ipc_server and ipc_server.has_peers(check_chans=True):
    ...
    await ipc_server.wait_for_no_more_peers()  # ← UNBOUNDED, blocks forever
```

`_no_more_peers` is an `Event` set only when
`server._peers` empties (see
`ipc/_server.py:526-530`). If ANY peer-handler is
stuck (the 5 unclosed loops from the earlier pass),
it keeps its channel in `server._peers`, so the
event never fires, so the wait hangs.

### Applied fix (partial, landed as defensive-in-depth)

`tractor/runtime/_runtime.py:1981` —
`wait_for_no_more_peers()` call now wrapped in
`trio.move_on_after(3.0)` + a warning log when the
timeout fires. Commented with the full rationale.

**Verified:** with this fix, ALL 15 actors reach
`async_main: RETURNING` cleanly (up from 10/15
reaching end before).

**Unfortunately:** the test still hangs past 45s
total — meaning there's YET ANOTHER unbounded wait
downstream of `async_main`. The bounded
`wait_for_no_more_peers` unblocks one level, but
the cascade has another level above it.

### Candidates for the remaining hang

1. `open_root_actor`'s own finally / post-
   `async_main` flow in `_root.py` — specifically
   `await actor.cancel(None)` which has its own
   internal waits.
2. The `trio.run()` itself doesn't return even
   after the root task completes because trio's
   nursery still has background tasks running.
3. Maybe `_serve_ipc_eps`'s finally has an await
   that blocks when peers aren't clearing.

### Current stance

- Defensive `wait_for_no_more_peers` bound landed
  (good hygiene regardless). Revealing a real
  deadlock-avoidance gap in tractor's cleanup.
- Test still hangs → skip-mark restored on
  `test_nested_multierrors[subint_forkserver]`.
- The full chain of unbounded waits needs another
  session of drilling, probably at
  `open_root_actor` / `actor.cancel` level.

### Summary of this investigation's wins

1. **FD hygiene fix** (`_close_inherited_fds`) —
   correct, closed orphan-SIGINT sibling issue.
2. **pidfd-based `_ForkedProc.wait`** — cancellable,
   matches trio_proc pattern.
3. **`_parent_chan_cs` wiring** —
   `Actor.cancel()` now breaks the shielded parent-
   chan `process_messages` loop.
4. **`wait_for_no_more_peers` bounded** —
   prevents the actor-level finally hang.
5. **Ruled-out hypotheses:** tree-kill missing
   (wrong), stuck socket recv (wrong).
6. **Pinpointed remaining unknown:** at least one
   more unbounded wait in the teardown cascade
   above `async_main`. Concrete candidates
   enumerated above.

## Update — 2026-04-23 (VERY late): pytest capture pipe IS the final gate

After landing fixes 1-4 and instrumenting every
layer down to `tractor_test`'s `trio.run(_main)`:

**Empirical result: with `pytest -s` the test PASSES
in 6.20s.** Without `-s` (default `--capture=fd`) it
hangs forever.

DIAG timeline for the root pytest PID (with `-s`
implied from later verification):

```
tractor_test: about to trio.run(_main)
open_root_actor: async_main task started, yielding to test body
_main: about to await wrapped test fn
_main: wrapped RETURNED cleanly        ← test body completed!
open_root_actor: about to actor.cancel(None)
Actor.cancel ENTER req_chan=False
Actor.cancel RETURN
open_root_actor: actor.cancel RETURNED
open_root_actor: outer FINALLY
open_root_actor: finally END (returning from ctxmgr)
tractor_test: trio.run FINALLY (returned or raised)  ← trio.run fully returned!
```

`trio.run()` fully returns. The test body itself
completes successfully (pytest.raises absorbed the
expected `BaseExceptionGroup`). What blocks is
**pytest's own stdout/stderr capture** — under
`--capture=fd` default, pytest replaces the parent
process's fd 1,2 with pipe write-ends it's reading
from. Fork children inherit those pipe fds
(because `_close_inherited_fds` correctly preserves
stdio). High-volume subactor error-log tracebacks
(7+ actors each logging multiple
`RemoteActorError`/`ExceptionGroup` tracebacks on
the error-propagation cascade) fill the 64KB Linux
pipe buffer. Subactor writes block. Subactor can't
progress. Process doesn't exit. Parent's
`_ForkedProc.wait` (now pidfd-based and
cancellable, but nothing's cancelling here since
the test body already completed) keeps the pipe
reader alive... but pytest isn't draining its end
fast enough because test-teardown/fixture-cleanup
is in progress.

**Actually** the exact mechanism is slightly
different: pytest's capture fixture MIGHT be
actively reading, but faster-than-writer subactors
overflow its internal buffer. Or pytest might be
blocked itself on the finalization step.

Either way, `-s` conclusively fixes it.

### Why I ruled this out earlier (and shouldn't have)

Earlier in this investigation I tested
`test_nested_multierrors` with/without `-s` and
both hung. That's because AT THAT TIME, fixes 1-4
weren't all in place yet. The test was hanging at
multiple deeper levels long before reaching the
"generate lots of error-log output" phase. Once
the cascade actually tore down cleanly, enough
output was produced to hit the capture-pipe limit.

**Classic order-of-operations mistake in
debugging:** ruling something out too early based
on a test that was actually failing for a
different reason.

### Fix direction (next session)

Redirect subactor stdout/stderr to `/dev/null` (or
a session-scoped log file) in the fork-child
prelude, right after `_close_inherited_fds()`. This
severs the inherited pytest-capture pipes and lets
subactor output flow elsewhere. Under normal
production use (non-pytest), stdout/stderr would
be the TTY — we'd want to keep that. So the
redirect should be conditional or opt-in via the
`child_sigint`/proc_kwargs flag family.

Alternative: document as a gotcha and recommend
`pytest -s` for any tests using the
`subint_forkserver` backend with multi-level actor
trees. Simpler, user-visible, no code change.

### Current state

- Skip-mark on `test_nested_multierrors[subint_forkserver]`
  restored with reason pointing here.
- Test confirmed passing with `-s` after all 4
  cascade fixes applied.
- The 4 cascade fixes are NOT wasted — they're
  correct hardening regardless of the capture-pipe
  issue, AND without them we'd never reach the
  "actually produces enough output to fill the
  pipe" state.

## Stopgap (landed)

`test_nested_multierrors` skip-marked under
`subint_forkserver` via
`@pytest.mark.skipon_spawn_backend('subint_forkserver',
reason='...')`, cross-referenced to this doc. Mark
should be dropped once the peer-channel-loop exit
issue is fixed.

## References

- `tractor/spawn/_subint_forkserver.py::fork_from_worker_thread`
  — the primitive whose post-fork FD hygiene is
  probably the culprit.
- `tractor/spawn/_subint_forkserver.py::subint_forkserver_proc`
  — the backend function that orchestrates the
  graceful cancel path hitting this bug.
- `tractor/spawn/_subint_forkserver.py::_ForkedProc`
  — the `trio.Process`-compatible shim; NOT the
  failing component (confirmed via thread-dump).
- `tests/test_cancellation.py::test_nested_multierrors`
  — the test that surfaced the hang.
- `ai/conc-anal/subint_forkserver_orphan_sigint_hang_issue.md`
  — sibling hang class; probably same underlying
  fork-FD-inheritance root cause.
- tractor issue #379 — subint backend tracking.
