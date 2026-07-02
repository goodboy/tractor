# `_ria_nursery` removal plan (issue #477 follow-up)

Goal: drop the secondary "run-in-actor" spawn nursery (and
friends) from `ActorNursery`/spawn internals, now that
`tractor.to_actor.run()` delivers one-shot semantics purely on
the daemon-spawn + portal primitives.

## Verified machinery map (2026-07-02, wkt @ a34aaf98)

The entire mechanism is 4 files:

- `runtime/_supervise.py`
  - `ActorNursery.__init__(.., ria_nursery, ..)` stores
    `._ria_nursery` (:202, :238); sole read is
    `run_in_actor()` passing `nursery=self._ria_nursery`
    (:442) into `start_actor()`'s `nursery:
    trio.Nursery|None` escape-hatch param (:305, :367).
  - `._cancel_after_result_on_exit: set` (:244) marks ria
    portals (:457).
  - `_open_and_supervise_one_cancels_all_nursery()` nests
    `da_nursery` (:609) around `ria_nursery` (:622); the
    `finally:` at the ria->da boundary (:747-766) raises
    collected `errors` (single exc or BEG).
- `runtime/_portal.py`
  - `._expect_result_ctx` (:112) set by `_submit_for_result()`
    (:142, sole caller `run_in_actor()`); consumed by
    `wait_for_result()` (:167) + deprecated `result()` (:220).
    The `None` branch (:184-196) returns the `NoResult`
    sentinel (`_exceptions.py:1164`).
- `spawn/_spawn.py`
  - `exhaust_portal()` (:129): awaits
    `portal.wait_for_result()`, CATCHES+RETURNS any exc
    (never raises).
  - `cancel_on_completion()` (:177): `exhaust_portal()` ->
    on exc-result stash `errors[uid] = result` (:203) ->
    ALWAYS `portal.cancel_actor()` (:218).
- `spawn/_trio.py` (:195-222) + `spawn/_mp.py` (:187-213),
  identical shape: after shielded
  `await an._join_procs.wait()`, open a per-child local
  nursery; IFF `portal in an._cancel_after_result_on_exit`
  start `cancel_on_completion` alongside `soft_kill()`; when
  `soft_kill` returns first, `nursery.cancel_scope.cancel()`
  reaps the result-waiter.

## The load-bearing semantic (already-deferred errors)

Remote ria-child errors NEVER raise into `ria_nursery`:

1. reaper tasks only START after `_join_procs.set()` (block
   exit or the inner error handler),
2. `exhaust_portal` swallows the exc into a return value,
3. `cancel_on_completion` stashes it in `errors` + cancels
   that child,
4. the ria->da `finally:` re-raises collected `errors` (and
   `an.cancel()`s any daemon stragglers).

So mid-block there is NO error propagation from ria children
(unless user code explicitly `await portal.wait_for_result()`s)
— the two-nursery nesting only sequences "reap ria results
BEFORE blocking on daemon join". A single-nursery impl only
needs to preserve that sequencing, not any ASAP-cancel
behavior.

## Target design

### step A: single-nursery `run_in_actor()` (mechanical)

- `run_in_actor()` spawns via the DEFAULT (`_da_nursery`)
  path — drop `nursery=self._ria_nursery`.
- rename `._cancel_after_result_on_exit` ->
  `._ria_portals: dict[portal, Actor]` (need the subactor ref
  for `cancel_on_completion`).
- move reaper start-up OUT of the backends into
  `_open_and_supervise...`: immediately after EACH
  `an._join_procs.set()` call-site (happy path :642, inner
  error handler :661), start one
  `cancel_on_completion(portal, subactor, errors)` task per
  ria portal into `da_nursery`, then (happy path only)
  `await` their completion BEFORE falling out of the
  `try:`/`finally:` that raises `errors` — e.g. gather in a
  dedicated inner `trio.open_nursery()` block replacing
  today's `ria_nursery` join point.
- delete the membership branch + local reaper nursery from
  `_trio.py`/`_mp.py` (keep the `soft_kill()` call; the
  per-child local nursery collapses to just `soft_kill`).
- `_trio.py:310` `_children.pop()` etc. unchanged.

### step B: delete the plumbing

- `_open_and_supervise...`: drop the inner
  `ria_nursery` + merge its `except BaseException` classify
  logic into ONE handler on the (now single) nursery scope;
  `ActorNursery.__init__` loses the `ria_nursery` param.
- `start_actor()` loses the `nursery:` escape-hatch param
  (the :302-304 TODO).
- backends: no more `_cancel_after_result_on_exit` refs.

### step C: (separate PRs) deprecate + migrate + excise

- migrate in-repo `.run_in_actor()` usage to
  `to_actor.run()`: tests 46 hits/9 files (test_cancellation
  15, test_infected_asyncio 10, test_spawning 8, registrar 3,
  adv_streaming 4, pubsub 2, rpc 1, runtime 1), examples 28
  hits/13 files (debugging/* dominate), docs 20 hits/8 rst
  files. NOTE: many sites also use deprecated
  `Portal.result()`/`wait_for_result()` — these die with
  `_expect_result_ctx`, so migration must land FIRST.
- add `DeprecationWarning` to `run_in_actor()` (+
  `_submit_for_result`/`wait_for_result`).
- final excision: `run_in_actor()`, `_submit_for_result`,
  `_expect_result_ctx`, `wait_for_result`/`result`,
  `exhaust_portal`, `cancel_on_completion`, `NoResult`.

## Risk register

1. hard-killed ria child: today the backend-local
   `nursery.cancel_scope.cancel()` discards a still-parked
   reaper when the proc dies first; a da_nursery-hosted
   reaper instead sees the transport break ->
   `exhaust_portal` returns a `TransportClosed`-ish exc ->
   NEW entry in `errors` that today gets discarded. Guard:
   reap-gather block must cancel remaining reapers once all
   ria procs are dead, or filter transport-death excs for
   already-`cancel_called` children.
2. error-path ordering: inner handler today sets
   `_join_procs` THEN `an.cancel()`; reapers race the
   cancel-RPC. Keep that ordering when moving reaper spawn.
3. debugger interplay: `maybe_wait_for_debugger()` calls
   (:654, :730) must stay BEFORE any reap/cancel issuance.
4. `errors` double-entry: local body error (:646) + child's
   relayed exc (via reaper) can both land for the same
   scenario -> BEG shape changes vs today? (today has the
   same dual-write sites; keep behavior identical.)
5. mp backend parity: mirror every `_trio.py` edit in
   `_mp.py` (identical block).

## Step-A first-probe findings (2026-07-02, WIP in tree)

Step A is IMPLEMENTED (uncommitted):
`run_in_actor()` spawns via da_nursery; new
`_supervise._reap_ria_portals()` helper; reap awaited after
happy-path `_join_procs.set()`; error-path runs reap
CONCURRENT with `an.cancel()` in the shielded block;
backends stripped of the membership branch + per-child
reaper nursery (+ dead imports).

Probe history (trio backend):
- `tests/test_to_actor.py` + `tests/test_spawning.py`:
  20/20 PASS — incl. all `run_in_actor()` result
  round-trips + `test_remote_error` (single erroring child,
  body re-raise -> inner error path).
- FIRST attempt ran the error-path reap CONCURRENT with
  `an.cancel()` (mimicking the old backend-side race):
  `test_cancellation.py::test_multierror` (2 erroring ria
  children, body re-raises one) DEADLOCKED. Root cause per
  the sequencing fix below: reap + cancel must NOT race at
  this layer (suspected `._children` pop-during-iteration
  and/or double-cancel RPC wedge; not fully root-caused
  since the fix removes the race wholesale).
- FIX (2nd attempt, current impl): error path SEQUENCES:
  (1) snapshot ria `(portal, subactor)` pairs (backend
  `finally`s pop `._children` as procs reap), (2)
  `await an.cancel()`, (3) bounded reap over the snapshot.
  Bound was first 3s -> blew the `fail_after` deadline in
  `test_cancel_while_childs_child_in_sync_sleep` (hard-
  killed grandchild never relays => reaper parks the full
  bound). Tightened to 0.5s: anything collectable is
  already queued in the local ctx (relayed BEFORE the
  cancel); a parked reaper self-cleans (`trio.Cancelled`
  results are never stashed).
- RESULT: `tests/test_cancellation.py` FULLY GREEN
  (20 passed, 1 xfailed, 77s); full-suite gate run kicked
  off same session (see final report/next session).

Remaining risk: on slow CI a relayed-but-undelivered error
racing the 0.5s bound could drop an `errors` entry
(BEG-shape flake); if observed, scale the bound via the
`cpu_perf_headroom()`-style approach or peek
`Portal._final_result_msg`/ctx queue state instead of
time-bounding.

## Step-B outcome (2026-07-02, done in tree)

Step A landed as `5cd190c5` (code) + `99310269` (docs).
Step B implemented on top (uncommitted):

- `._ria_nursery` is GONE — the inner
  `async with (collapse_eg(), trio.open_nursery() as
  ria_nursery)` layer in
  `_open_and_supervise_one_cancels_all_nursery` is deleted;
  `da_nursery` is now the single nursery for ALL subactors.
- `ActorNursery.__init__` drops the `ria_nursery` param +
  the `self._ria_nursery` attr; `start_actor()` drops its
  `nursery=` escape-hatch param (uses `self._da_nursery`
  directly).
- `._cancel_after_result_on_exit` STAYS — it's the
  ria-child discriminator for `_reap_ria_portals()`.

Deliberately NOT done (deferred to its own higher-risk PR,
flagged with a TODO at the outer `except`): merging the two
error handlers into one. Rationale — collapsing the empty
nursery is provably behavior-preserving (a zero-task
`trio.open_nursery()` only adds a checkpoint), whereas the
inner `except BaseException` (swallow-into-`errors`) and
outer `except (...)` (re-raise, safety-net for the inner
handler's own non-shielded awaits) have DIFFERENT
semantics; merging changes error/cancel propagation and
wants isolated review + its own gate. Both handlers are
kept, now nested directly under the single nursery.

Why the collapse is safe: post-step-A NOTHING spawns into
`ria_nursery` (its only reader, `run_in_actor`'s
`nursery=self._ria_nursery`, was removed in A; the stored
attr was never read again). So the layer was pure dead
weight.

Gate (trio backend, all 0-failure):
- targeted set (`test_cancellation test_spawning test_local
  test_rpc test_to_actor`) = 49 passed, 1 xfailed.
- tail set (`test_reg_err_types remote_exc_relay
  resource_cache ringbuf root_infect_asyncio root_runtime
  runtime shm task_broadcasting trioisms trionics/`) = 63
  passed, 1 skipped, 5 xfailed.
- full-suite head ~73% (subdirs + `test_2way`..`test_pubsub`)
  = 303 passed before the known-flaky `test_dynamic_pub_sub`
  TooSlowError stall (pre-existing; same hang in the step-A
  full run). Suite ran slow this session (~13min vs 555s
  cold, likely thermal from back-to-back runs), never
  completing within an 800s bound — but split across the
  above three runs EVERY module passed under step B.

## Verification gate

- `tests/test_cancellation.py test_spawning.py test_local.py
  test_rpc.py` on `trio` + `mp_spawn` + `mp_forkserver`
  backends, then full suite; `tests/devx/test_debugger.py`
  for risk 3.
