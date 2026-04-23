# `subint_forkserver` backend: orphaned-subactor SIGINT wedged in `epoll_wait`

Follow-up to the Phase C `subint_forkserver` spawn-backend
PR (see `tractor.spawn._subint_forkserver`, issue #379).
Surfaced by the xfail'd
`tests/spawn/test_subint_forkserver.py::test_orphaned_subactor_sigint_cleanup_DRAFT`.

Related-but-distinct from
`subint_cancel_delivery_hang_issue.md` (orphaned-channel
park AFTER subint teardown) and
`subint_sigint_starvation_issue.md` (GIL-starvation,
SIGINT never delivered): here the SIGINT IS delivered,
trio's handler IS installed, but trio's event loop never
wakes — so the KBI-at-checkpoint → `_trio_main` catch path
(which is the runtime's *intentional* OS-cancel design)
never fires.

## TL;DR

When a `subint_forkserver`-spawned subactor is orphaned
(parent `SIGKILL`'d, no IPC cancel path available) and then
externally `SIGINT`'d, the subactor hangs in
`trio/_core/_io_epoll.py::get_events` (epoll_wait)
indefinitely — even though:

1. `threading.current_thread() is threading.main_thread()`
   post-fork (CPython 3.14 re-designates correctly).
2. Trio's SIGINT handler IS installed in the subactor
   (`signal.getsignal(SIGINT)` returns
   `<function KIManager.install.<locals>.handler at 0x...>`).
3. The kernel does deliver SIGINT — the signal arrives at
   the only thread in the process (the fork-inherited
   worker which IS now "main" per Python).

Yet `epoll_wait` does not return. Trio's wakeup-fd mechanism
— the machinery that turns SIGINT into an epoll-wake — is
somehow not firing the wakeup. Until that's fixed, the
intentional "KBI-as-OS-cancel" path in
`tractor/spawn/_entry.py::_trio_main:164` is unreachable
for forkserver-spawned subactors whose parent dies.

## Symptom

Test: `tests/spawn/test_subint_forkserver.py::test_orphaned_subactor_sigint_cleanup_DRAFT`
(currently marked `@pytest.mark.xfail(strict=True)`).

1. Harness subprocess brings up a tractor root actor +
   one `run_in_actor(_sleep_forever)` subactor via
   `try_set_start_method('subint_forkserver')`.
2. Harness prints `CHILD_PID` (subactor) and
   `PARENT_READY` (root actor) markers to stdout.
3. Test `os.kill(parent_pid, SIGKILL)` + `proc.wait()`
   to fully reap the root-actor harness.
4. Child (now reparented to pid 1) is still alive.
5. Test `os.kill(child_pid, SIGINT)` and polls
   `os.kill(child_pid, 0)` for up to 10s.
6. **Observed**: the child is still alive at deadline —
   SIGINT did not unwedge the trio loop.

## What the "intentional" cancel path IS

`tractor/spawn/_entry.py::_trio_main:157-186` —

```python
try:
    if infect_asyncio:
        actor._infected_aio = True
        run_as_asyncio_guest(trio_main)
    else:
        trio.run(trio_main)

except KeyboardInterrupt:
    logmeth = log.cancel
    exit_status: str = (
        'Actor received KBI (aka an OS-cancel)\n'
        ...
    )
```

The "KBI == OS-cancel" mapping IS the runtime's
deliberate, documented design. An OS-level SIGINT should
flow as: kernel → trio handler → KBI at trio checkpoint
→ unwinds `async_main` → surfaces at `_trio_main`'s
`except KeyboardInterrupt:` → `log.cancel` + clean `rc=0`.

**So fixing this hang is not "add a new SIGINT behavior" —
it's "make the existing designed behavior actually fire in
this backend config".** That's why option (B) ("fix root
cause") is aligned with existing design intent, not a
scope expansion.

## Evidence

### Positive control: standalone fork-from-worker + `trio.run(sleep_forever)` + SIGINT WORKS

```python
import os, signal, time, trio
from tractor.spawn._subint_forkserver import (
    fork_from_worker_thread, wait_child,
)

def child_target() -> int:
    async def _main():
        try:
            await trio.sleep_forever()
        except KeyboardInterrupt:
            print('CHILD: caught KBI — trio SIGINT works!')
            return
    trio.run(_main)
    return 0

pid = fork_from_worker_thread(child_target, thread_name='trio-sigint-test')
time.sleep(1.0)
os.kill(pid, signal.SIGINT)
wait_child(pid)
```

Result: `CHILD: caught KBI — trio SIGINT works!` + clean
exit. So the fork-child + trio signal plumbing IS healthy
in isolation. The hang appears only with the full tractor
subactor runtime on top.

### Negative test: full tractor subactor + orphan-SIGINT

Equivalent to the xfail test. Traceback dump via
`faulthandler.register(SIGUSR1, all_threads=True)` at the
stuck moment:

```
Current thread 0x00007... [subint-forkserv] (most recent call first):
  File ".../trio/_core/_io_epoll.py", line 245 in get_events
  File ".../trio/_core/_run.py", line 2415 in run
  File "tractor/spawn/_entry.py", line 162 in _trio_main
  File "tractor/_child.py", line 72 in _actor_child_main
  File "tractor/spawn/_subint_forkserver.py", line 650 in _child_target
  File "tractor/spawn/_subint_forkserver.py", line 308 in _worker
  File ".../threading.py", line 1024 in run
```

### Thread + signal-mask inventory of the stuck subactor

Single thread (`tid == pid`, comm `'subint-forkserv'`,
which IS `threading.main_thread()` post-fork):

```
SigBlk:  0000000000000000  # nothing blocked
SigIgn:  0000000001001000  # SIGPIPE etc (Python defaults)
SigCgt:  0000000108000202  # bit 1 = SIGINT caught
```

Bit 1 set in `SigCgt` → SIGINT handler IS installed. So
trio's handler IS in place at the kernel level — not a
"handler missing" situation.

### Handler identity

Inside the subactor's RPC body, `signal.getsignal(SIGINT)`
returns `<function KIManager.install.<locals>.handler at
0x...>` — trio's own `KIManager` handler. tractor's only
SIGINT touches are `signal.getsignal()` *reads* (to stash
into `debug.DebugStatus._trio_handler`); nothing writes
over trio's handler outside the debug-REPL shielding path
(`devx/debug/_tty_lock.py::shield_sigint`) which isn't
engaged here (no debug_mode).

## Ruled out

- **GIL starvation / signal-pipe-full** (class A,
  `subint_sigint_starvation_issue.md`): subactor runs on
  its own GIL (separate OS process), not sharing with the
  parent → no cross-process GIL contention. And `strace`-
  equivalent in the signal mask shows SIGINT IS caught,
  not queued.
- **Orphaned channel park** (`subint_cancel_delivery_hang_issue.md`):
  different failure mode — that one has trio iterating
  normally and getting wedged on an orphaned
  `chan.recv()` AFTER teardown. Here trio's event loop
  itself never wakes.
- **Tractor explicitly catching + swallowing KBI**:
  greppable — the one `except KeyboardInterrupt:` in the
  runtime is the INTENTIONAL cancel-path catch at
  `_trio_main:164`. `async_main` uses `except Exception`
  (not BaseException), so KBI should propagate through
  cleanly if it ever fires.
- **Missing `signal.set_wakeup_fd` (main-thread
  restriction)**: post-fork, the fork-worker thread IS
  `threading.main_thread()`, so trio's main-thread check
  passes and its wakeup-fd install should succeed.

## Root cause hypothesis (unverified)

The SIGINT handler fires but trio's wakeup-fd write does
not wake `epoll_wait`. Candidate causes, ranked by
plausibility:

1. **Wakeup-fd lifecycle race around tractor IPC setup.**
   `async_main` spins up an IPC server + `process_messages`
   loops early. Somewhere in that path the wakeup-fd that
   trio registered with its epoll instance may be
   closed/replaced/clobbered, so subsequent SIGINT writes
   land on an fd that's no longer in the epoll set.
   Evidence needed: compare
   `signal.set_wakeup_fd(-1)` return value inside a
   post-tractor-bringup RPC body vs. a pre-bringup
   equivalent. If they differ, that's it.
2. **Shielded cancel scope around `process_messages`.**
   The RPC message loop is likely wrapped in a trio cancel
   scope; if that scope is `shield=True` at any outer
   layer, KBI scheduled at a checkpoint could be absorbed
   by the shield and never bubble out to `_trio_main`.
3. **Pre-fork wakeup-fd inheritance.** trio in the PARENT
   process registered a wakeup-fd with its own epoll. The
   child inherits the fd number but not the parent's
   epoll instance — if tractor/trio re-uses the parent's
   stale fd number anywhere, writes would go to a no-op
   fd. (This is the least likely — `trio.run()` on the
   child calls `KIManager.install` which should install a
   fresh wakeup-fd from scratch.)

## Cross-backend scope question

**Untested**: does the same orphan-SIGINT hang reproduce
against the `trio_proc` backend (stock subprocess + exec)?
If yes → pre-existing tractor bug, independent of
`subint_forkserver`. If no → something specific to the
fork-from-worker path (e.g. inherited fds, mid-epoll-setup
interference).

**Quick repro for trio_proc**:

```python
# save as /tmp/trio_proc_orphan_sigint_repro.py
import os, sys, signal, time, glob
import subprocess as sp

SCRIPT = '''
import os, sys, trio, tractor
async def _sleep_forever():
    print(f"CHILD_PID={os.getpid()}", flush=True)
    await trio.sleep_forever()

async def _main():
    async with (
        tractor.open_root_actor(registry_addrs=[("127.0.0.1", 12350)]),
        tractor.open_nursery() as an,
    ):
        await an.run_in_actor(_sleep_forever, name="sf-child")
        print(f"PARENT_READY={os.getpid()}", flush=True)
        await trio.sleep_forever()

trio.run(_main)
'''

proc = sp.Popen(
    [sys.executable, '-c', SCRIPT],
    stdout=sp.PIPE, stderr=sp.STDOUT,
)
# parse CHILD_PID + PARENT_READY off proc.stdout ...
# SIGKILL parent, SIGINT child, poll.
```

If that hangs too, open a broader issue; if not, this is
`subint_forkserver`-specific (likely fd-inheritance-related).

## Why this is ours to fix (not CPython's)

- Signal IS delivered (`SigCgt` bitmask confirms).
- Handler IS installed (trio's `KIManager`).
- Thread identity is correct post-fork.
- `_trio_main` already has the intentional KBI→clean-exit
  path waiting to fire.

Every CPython-level precondition is met. Something in
tractor's runtime or trio's integration with it is
breaking the SIGINT→wakeup→event-loop-wake pipeline.

## Possible fix directions

1. **Audit the wakeup-fd across tractor's IPC bringup.**
   Add a trio startup hook that captures
   `signal.set_wakeup_fd(-1)` at `_trio_main` entry,
   after `async_main` enters, and periodically — assert
   it's unchanged. If it moves, track down the writer.
2. **Explicit `signal.set_wakeup_fd` reset after IPC
   setup.** Brute force: re-install a fresh wakeup-fd
   mid-bringup. Band-aid, but fast to try.
3. **Ensure no `shield=True` cancel scope envelopes the
   RPC-message-loop / IPC-server task.** If one does,
   KBI-at-checkpoint never escapes.
4. **Once fixed, the `child_sigint='trio'` mode on
   `subint_forkserver_proc`** becomes effectively a no-op
   or a doc-only mode — trio's natural handler already
   does the right thing. Might end up removing the flag
   entirely if there's no behavioral difference between
   modes.

## Current workaround

None; `child_sigint` defaults to `'ipc'` (IPC cancel is
the only reliable cancel path today), and the xfail test
documents the gap. Operators hitting orphan-SIGINT get a
hung process that needs `SIGKILL`.

## Reproducer

Inline, standalone (no pytest):

```python
# save as /tmp/orphan_sigint_repro.py  (py3.14+)
import os, sys, signal, time, glob, trio
import tractor
from tractor.spawn._subint_forkserver import (
    fork_from_worker_thread,
)

async def _sleep_forever():
    print(f'SUBACTOR[{os.getpid()}]', flush=True)
    await trio.sleep_forever()

async def _main():
    async with (
        tractor.open_root_actor(
            registry_addrs=[('127.0.0.1', 12349)],
        ),
        tractor.open_nursery() as an,
    ):
        await an.run_in_actor(_sleep_forever, name='sf-child')
        await trio.sleep_forever()

def child_target() -> int:
    from tractor.spawn._spawn import try_set_start_method
    try_set_start_method('subint_forkserver')
    trio.run(_main)
    return 0

pid = fork_from_worker_thread(child_target, thread_name='repro')
time.sleep(3.0)

# find the subactor pid via /proc
children = []
for path in glob.glob(f'/proc/{pid}/task/*/children'):
    with open(path) as f:
        children.extend(int(x) for x in f.read().split() if x)
subactor_pid = children[0]

# SIGKILL root → orphan the subactor
os.kill(pid, signal.SIGKILL)
os.waitpid(pid, 0)
time.sleep(0.3)

# SIGINT the orphan — should cause clean trio exit
os.kill(subactor_pid, signal.SIGINT)

# poll for exit
for _ in range(100):
    try:
        os.kill(subactor_pid, 0)
        time.sleep(0.1)
    except ProcessLookupError:
        print('HARNESS: subactor exited cleanly ✔')
        sys.exit(0)
os.kill(subactor_pid, signal.SIGKILL)
print('HARNESS: subactor hung — reproduced')
sys.exit(1)
```

Expected (current): `HARNESS: subactor hung — reproduced`.

After fix: `HARNESS: subactor exited cleanly ✔`.

## References

- `tractor/spawn/_entry.py::_trio_main:157-186` — the
  intentional KBI→clean-exit path this bug makes
  unreachable.
- `tractor/spawn/_subint_forkserver` — the backend whose
  orphan cancel-robustness this blocks.
- `tests/spawn/test_subint_forkserver.py::test_orphaned_subactor_sigint_cleanup_DRAFT`
  — the xfail'd reproducer in the test suite.
- `ai/conc-anal/subint_cancel_delivery_hang_issue.md` —
  sibling "orphaned channel park" hang (different class).
- `ai/conc-anal/subint_sigint_starvation_issue.md` —
  sibling "GIL starvation SIGINT drop" hang (different
  class).
- tractor issue #379 — subint backend tracking.
