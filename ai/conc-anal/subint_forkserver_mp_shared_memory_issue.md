# `subint_forkserver` × `multiprocessing.SharedMemory`: fork-inherited `resource_tracker` fd

Surfaced by `tests/test_shm.py` under
`--spawn-backend=subint_forkserver`. Two distinct
failure modes, one root cause:
**`multiprocessing.resource_tracker` is fork-without-exec
unsafe** (canonical CPython class — bpo-38119, bpo-45209).

**Status: resolved by `tractor/ipc/_mp_bs.py` +
`tractor/ipc/_shm.py` changes (see "Resolution" below).
This doc kept as the
post-mortem / decision record.**

## TL;DR

`mp.shared_memory.SharedMemory` registers each shm
allocation with the per-process
`multiprocessing.resource_tracker` singleton. The
tracker is a daemon process started lazily; the
parent owns a unix-pipe-fd to it. When the parent
forks-without-execing into a `subint_forkserver`
child, the child inherits that fd — but it refers to
the *parent's* tracker, which the child has no
business writing to.

Two manifestations under the original (pre-fix) code:

1. **`test_child_attaches_alot`** — child loops 1000×
   `attach_shm_list()`. First `mp.SharedMemory` call
   in the child triggers
   `resource_tracker._ensure_running_and_write` →
   `_teardown_dead_process` → `os.close(self._fd)` on
   an fd the child should never have touched. Surfaces
   as `OSError: [Errno 9] Bad file descriptor`
   wrapped in `tractor.RemoteActorError`.

2. **`test_parent_writer_child_reader[*]`** — first
   parametrize variant "passes" (with
   `resource_tracker: leaked shared_memory` warning)
   because nobody ever cleans up `/shm_list`.
   Subsequent variants then fail with
   `FileExistsError: '/shm_list'` because the leak
   persists across the parametrize loop and forkserver
   children can't `shm_open(create=True)` an existing
   key.

Trio backend (`mp_spawn`-style) doesn't surface this:
each subactor `exec`s a fresh interpreter →
independent resource tracker per subactor → no
inherited-fd issue, and the test's pre-existing leak
gets masked by the per-process tracker reset.

Under `subint_forkserver`, the child is `os.fork()`'d
from a worker thread (no `exec`) → inherits parent's
`mp.resource_tracker._resource_tracker._fd` → EBADF
/ cross-talk on first `mp.SharedMemory` op.

## Resolution

We side-step the broken upstream machinery entirely
rather than try to make it fork-safe. Two-part fix
landed (commits to follow this doc):

### 1. `tractor/ipc/_mp_bs.py::disable_mantracker()`
   — unconditional disable

The previous "3.13+ short-circuit" path used
`partial(SharedMemory, track=False)` to opt-out of
registration on 3.13+. The `track=False` switch is
necessary but not sufficient under fork: the
inherited tracker fd can still be touched indirectly
(e.g. through `_ensure_running_and_write`'s
self-check path).

The fix takes both belts AND suspenders:

- **Always** monkey-patch
  `mp.resource_tracker._resource_tracker` to a
  no-op `ManTracker` subclass whose
  `register`/`unregister`/`ensure_running` are all
  empty.
- **Always** wrap `SharedMemory` with
  `track=False`.

Result: the inherited tracker fd in the fork child
is still inherited (fd is a kernel object; we can't
un-inherit it across fork) but **nothing in the
shm code path will ever try to use it** — both the
tracker singleton and the per-allocation registration
are short-circuited.

### 2. `tractor/ipc/_shm.py::open_shm_list()`
   — own the cleanup

Without `mp.resource_tracker`, nobody else will
unlink leaked segments at process exit. tractor
already controls actor lifecycle, so we register
unlink on the actor's lifetime stack:

```python
def try_unlink():
    try:
        shml.shm.unlink()
    except FileNotFoundError as fne:
        log.exception(...)  # benign sibling-already-cleaned race

actor.lifetime_stack.callback(try_unlink)
```

The `FileNotFoundError` swallow handles the case
where a sibling actor already unlinked the same
segment (legitimate race in shared-key setups).

## Why this is the right call

- **mp's tracker is widely criticized.** The
  in-tree comment "non-SC madness" predates this
  fix and matches CPython upstream's own discomfort
  (e.g. the per-context tracker design rework
  discussions in bpo-43475).
- **tractor already owns process lifecycle.** We
  have `actor.lifetime_stack`, `Portal.cancel_actor`,
  and the IPC cancel cascade. Adding mp's tracker
  on top buys nothing we can't do better ourselves.
- **Backend-uniform.** No special-casing per spawn
  backend. trio (`mp_spawn`-style), `subint_forkserver`,
  and the future `subint` all behave identically
  — register-time no-op, exit-time unlink-via-
  lifetime-stack.

## Trade-offs / known gaps

- **Crash-leaked segments.** If an actor segfaults
  or is `SIGKILL`'d before its lifetime stack runs,
  `/dev/shm/<key>` will leak. Mitigations:
  - `tractor-reap` (the new
    `scripts/tractor-reap` CLI) doesn't yet sweep
    `/dev/shm` — could extend it.
  - Higher-level apps using shm should pin a UUID
    into the key (the `'shml_<uuid>'` pattern in
    `test_child_attaches_alot`) so leaks are
    distinct per session and easy to GC out-of-band.
- **Cross-actor unlink races.** Two actors holding
  the same shm key racing on `unlink()` — handled
  by the `FileNotFoundError` swallow.
- **Crashes won't show up in mp's leak warning.**
  We've turned off `resource_tracker`, so the usual
  `resource_tracker: There appear to be N leaked
  shared_memory objects to clean up at shutdown`
  warning is gone too. If we ever want it back as
  a crash-detection signal, we'd need our own
  equivalent (walk the actor's `_shm_list_keys` set
  at root teardown, log any unfreed).

## Verification

```sh
# fixed under both backends:
./py314/bin/python -m pytest tests/test_shm.py \
    --spawn-backend=subint_forkserver
# 7 passed

./py314/bin/python -m pytest tests/test_shm.py \
    --spawn-backend=trio
# 7 passed (regression check)
```

## References

- CPython upstream issues:
  - https://bugs.python.org/issue38119 (fork
    + resource_tracker fd inheritance)
  - https://bugs.python.org/issue45209
    (SharedMemory + resource_tracker)
  - https://bugs.python.org/issue43475
    (per-context tracker rework discussion)
- Long-term alternative: migrate off
  `multiprocessing.shared_memory` entirely to
  `posix_ipc` (no tracker) or finish the
  `hotbaud`-based ringbuf transport. Not blocked on
  this fix — both are independently tracked.
