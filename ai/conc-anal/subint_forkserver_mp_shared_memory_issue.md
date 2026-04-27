# `subint_forkserver` × `multiprocessing.SharedMemory`: incompatible-by-mp-design

Surfaced by `tests/test_shm.py` under
`--spawn-backend=subint_forkserver`. Both test functions
fail with distinct symptoms that share one root cause:
**`multiprocessing.resource_tracker` is fork-without-exec
unsafe.**

## TL;DR

`mp.shared_memory.SharedMemory` registers each shm
allocation with the per-process
`multiprocessing.resource_tracker` singleton. The
tracker is a daemon process started lazily, and the
parent owns a unix-pipe-fd to it. When the parent
forks-without-execing into a `subint_forkserver`
child, the child inherits that fd — but the fd refers
to the *parent's* tracker, which the child has no
business writing to.

Two manifestations:

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
   key. Trio backend doesn't surface this because
   each subactor `exec`s a fresh interpreter →
   independent resource tracker per subactor → no
   inherited-fd issue, and the test's pre-existing
   leak is masked by the per-process tracker reset.

## Why trio backend works

Under `--spawn-backend=trio`, each subactor is born
via `python -m tractor._child` (full `execve`) →
fresh interpreter → fresh module-level globals →
`mp.resource_tracker._resource_tracker` is `None`
until first use → `mp.SharedMemory` constructs its
own tracker, talks to its own pipe-fd. No cross-
process fd inheritance.

Under `subint_forkserver`, the child is
`os.fork()`'d from a worker thread of the parent
(no `exec`) → inherits parent's
`mp.resource_tracker._resource_tracker._fd` →
EBADF / cross-talk on first `mp.SharedMemory`
operation in the child.

## Status

**Not a tractor bug.** This is the canonical
"fork-without-exec breaks `multiprocessing`
internals" class — see CPython issues:

- https://bugs.python.org/issue38119
- https://bugs.python.org/issue45209

Pure-`fork` start method has the same incompatibility;
that's why `mp` itself defaults to `spawn` on macOS
and `forkserver`/`spawn` on Linux post-3.14.

## Mitigation

`tests/test_shm.py` is module-marked with
`pytest.mark.skipon_spawn_backend('subint_forkserver',
'subint', reason=...)` pointing at this doc.

Two longer-term options if we ever want shm tests under
`subint_forkserver`:

1. **Reset the inherited tracker fd in the child
   prelude** —
   `tractor/spawn/_subint_forkserver.py::_child_target`
   already calls `_close_inherited_fds()`. We could
   additionally explicitly clear
   `multiprocessing.resource_tracker._resource_tracker`
   so the child re-creates a fresh tracker on first
   shm op. **Caveat**: this means each
   forkserver-subactor spawns its own resource-tracker
   daemon-process, multiplying daemon-proc count by
   subactor count. mp authors deliberately avoided
   this — the tracker is meant to be a per-mp-context
   singleton.

2. **Stop using `multiprocessing.shared_memory`** —
   migrate to `posix_ipc` directly (no resource
   tracker) or finish the `hotbaud`-based ringbuf
   transport that already supersedes shm in many
   `tractor` IPC paths.

Neither is in scope for the
`subint_forkserver`-backend-lands PR; both are tracked
out as future work.

## Reproducer

```sh
# fail mode 1 (EBADF on resource_tracker._fd):
./py314/bin/python -m pytest \
    tests/test_shm.py::test_child_attaches_alot \
    --spawn-backend=subint_forkserver --tb=short

# fail mode 2 (FileExistsError on /shm_list):
./py314/bin/python -m pytest \
    tests/test_shm.py::test_parent_writer_child_reader \
    --spawn-backend=subint_forkserver

# baseline (passes):
./py314/bin/python -m pytest \
    tests/test_shm.py --spawn-backend=trio
```
