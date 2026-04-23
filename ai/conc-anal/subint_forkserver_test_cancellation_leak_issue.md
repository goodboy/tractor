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

### Aside: `-s` flag changes behavior for peer-intensive tests

While exploring, noticed
`tests/test_context_stream_semantics.py` under
`--spawn-backend=subint_forkserver` hangs with
pytest's default `--capture=fd` but passes with
`-s` (`--capture=no`). Hypothesis (unverified): fork
children inherit pytest's capture pipe for stdout/
stderr (fds 1,2 — we preserve these in
`_close_inherited_fds`). When subactor logging is
verbose, the capture pipe buffer fills, writes block,
child can't progress, deadlock.

If confirmed, fix direction: redirect subactor
stdout/stderr to `/dev/null` (or a file) in
`_actor_child_main` so subactors don't hold pytest's
capture pipe open. Not a blocker on the main
peer-chan-loop investigation; deserves its own mini-
tracker.

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
