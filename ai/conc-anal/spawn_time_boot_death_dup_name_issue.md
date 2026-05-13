# Spawn-time boot-death (`rc=2`) under rapid same-name spawn against a registrar

## Symptom

Spawning N (≥4) sub-actors with the **same name** in tight
succession against a daemon registrar surfaces as
`ActorFailure: Sub-actor (...) died during boot (rc=2)
before completing parent-handshake`.

```
tests/discovery/test_multi_program.py
  ::test_dup_name_cancel_cascade_escalates_to_hard_kill[n_dups=4]
```

```
tractor._exceptions.ActorFailure:
  Sub-actor ('doggy', '<uuid>') died during boot (rc=2)
  before completing parent-handshake.
    proc: <_ForkedProc pid=<n> returncode=None>
```

The `proc` repr shows `returncode=None` because the repr is
captured before `proc.wait()` returns; the actual
`os.WEXITSTATUS == 2` is reported via `result['died']` in the
race-helper.

## When it surfaces

- N=2 (`n_dups=2`): **always passes**.
- N=4 (`n_dups=4`): **consistent fail** under both `tpt-proto=tcp`
  and `tpt-proto=uds`, MTF backend.
- N=8 (`n_dups=8`): **passes** (counter-intuitive — see "racing
  windows").
- Non-MTF backends: not yet exercised systematically.

## What previously masked it

Pre the spawn-time `wait_for_peer_or_proc_death` race-helper
(in `tractor.spawn._spawn`), the parent's `start_actor` flow
ended with a bare:

```python
event, chan = await ipc_server.wait_for_peer(uid)
```

That awaits an unsignalled `trio.Event` on `_peer_connected[uid]`.
If the sub-actor process **dies during boot** (before its
runtime executes the parent-callback handshake that sets the
event), the wait parks forever. The dead proc becomes a zombie
because no one ever calls `proc.wait()` to reap it.

In test contexts the failure presented as a hang or a much
later `trio.TooSlowError` from an outer `fail_after`. In
production it'd present as a parent that never makes progress
past `start_actor`. The death itself was silently masked.

## What surfaces it now

`tractor.spawn._spawn.wait_for_peer_or_proc_death` (used by
`_main_thread_forkserver_proc`) races the handshake-wait
against `proc.wait()`. The race-helper raises `ActorFailure`
on death-first instead of parking, exposing the rc=2.

## Hypothesis: registrar-side same-name contention

The test spawns N actors with name `doggy` sequentially:

```python
for i in range(n_dups):
    p: Portal = await an.start_actor('doggy')
    portals.append(p)
```

Each spawned doggy:

1. Forks via the forkserver.
2. Boots its runtime in `_actor_child_main`.
3. Connects back to the parent for handshake.
4. Connects to the daemon registrar to call `register_actor`.
5. Enters its RPC msg-loop.

Step (4) is where the same-name contention lives. The
registrar's `register_actor` (in
`tractor.discovery._registry`) accepts duplicate names
(stores `(name, uuid) -> addr`), but its internal bookkeeping
may have a non-trivial check (e.g. `wait_for_actor` resolution,
`_addrs2aids` map updates) that errors out under specific
ordering between the existing entry and the incoming one.

`rc=2 == os.WEXITSTATUS == 2` corresponds to `sys.exit(2)`
in the doggy process — typically reached via an unhandled
exception that's translated to exit code 2 by Python's top-
level (e.g. `argparse` errors use 2; `SystemExit(2)` etc.).
So the doggy is hitting an explicit exit path during
`register_actor` or just-after.

The non-monotonic shape (N=2 OK, N=4 BAD, N=8 OK) suggests a
specific timing window — likely "the 3rd register-RPC arrives
while the 1st-or-2nd is in some intermediate state". With
N=8, the additional procs widen the registration spread
enough that no two land in the conflicting window.

## Where to dig next

- Add per-actor logging in `_actor_child_main` and
  `register_actor` to surface the actual exception that
  triggers the rc=2 exit. Currently the doggy dies before
  the parent ever sees its stderr (forkserver doesn't
  marshal child stdio back).
- Race-test the registrar's `register_actor` /
  `unregister_actor` /  `wait_for_actor` against same-name
  concurrent calls in isolation (no spawn).
- Consider whether `register_actor` should be idempotent
  under same-name re-register or should explicitly reject
  same-name (and ideally with a clear `RemoteActorError`,
  not `sys.exit(2)`).

## Test-suite handling

Currently:

- `tests/discovery/test_multi_program.py
  ::test_dup_name_cancel_cascade_escalates_to_hard_kill[n_dups=4]`
  is `pytest.mark.xfail(strict=False, reason=...)` to keep
  the suite green while this issue is investigated.
- `n_dups=2` and `n_dups=8` continue to validate the
  cancel-cascade hard-kill escalation.

Once the underlying race is understood + fixed, drop the
xfail.

## Related work

- The cancel-cascade fix that introduced this regression
  test:
  `tractor/_exceptions.py:ActorTooSlowError`,
  `tractor/runtime/_supervise.py:_try_cancel_then_kill`,
  `tractor/runtime/_portal.py:Portal.cancel_actor(
   raise_on_timeout=...)`.
- The spawn-time death-detection that exposed this:
  `tractor/spawn/_spawn.py:wait_for_peer_or_proc_death`,
  used by `tractor/spawn/_main_thread_forkserver.py`.
