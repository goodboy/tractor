# `to_actor.open_taskman()` design + impl plan (issue #485)

Self-contained hand-off doc: everything needed to implement the
tracking issue https://github.com/goodboy/tractor/issues/485
without prior session context. Read alongside the (shorter)
issue body; where they differ THIS doc is the more detailed and
the issue is the contract.

## Context (why this exists)

Issue #477 removed the legacy non-blocking
`ActorNursery.run_in_actor()` (see PR #484, branch
`drop_ria_nursery`, + `ria_nursery_removal_plan.md` in this
dir): its "result at nursery-teardown" semantic required an
internal reap-cluster whose unowned result-waits produced an
unbounded-hang class. The blocking successor `to_actor.run()`
(PR #481) covers one-shots; the *deferred/non-blocking* shape
now needs a properly scoped home — an explicit supervision
scope for dynamically spawned remote tasks over a (flat)
subactor cluster.

Core principle distilled from the #477 arc: **every
non-blocking remote-task invocation must have an
explicitly-owned enclosing scope**. Hence NO `Portal.run_soon()`
public method (a connection handle must not own task lifetimes)
— the scope-owning object *is* the API.

## Prior art (all three MUST be mined)

1. **`hilevel_serman` branch** (tip `93d161bf`, 2024-12,
   4 commits): `tractor/hilevel/{__init__,_service.py}` — the
   `piker.service.Services` port. `open_service_mngr()`
   (actor-global singleton acm stacking `open_nursery()` +
   `trio.open_nursery()`), `ServiceMngr` dataclass w/
   `.start_service_task()` (local bg task w/ cs+done-event),
   `.start_service_ctx()` (bg-task-supervised remote ctx via
   `_open_and_supervise_service_ctx()`), `.start_service()`
   (spawn actor + service ctx), `.cancel_service[_task]()`.
2. **PR #363** (`oco_supervisor_prototype`, OPEN): the
   `trionics` "taskman" prototype — a `trio.Nursery`-like
   per-task-scope-manager via user-defined
   single-yield-generator hooks. Mine for the per-task
   scope-hook shape + naming; its in-code ref appears (typo'd
   as "#346") in `_service.py`'s
   "TODO, unify this interface with our `TaskManager` PR!".
3. **`piker.service._mngr.Services`** + the
   `piker.data.feed.open_feed()` `gather_contexts()` pyramid:
   the production usage this design must be able to replace.

Also related in-tree: `trionics.gather_contexts(mngrs, tn=None)`
(the static fan-out cousin) and the `to_actor.open_one_shot()`
single-task sketch in `ria_nursery_removal_plan.md`.

## `hilevel_serman` rebase + convergence map

Recon (as of `drop_ria_nursery` @ `c62f93a8`):

- 266 commits behind `main`; `tractor/hilevel/` is a NEW dir so
  NO file-level merge conflicts expected.
- imports only top-level names (`ActorNursery`, `Context`,
  `Portal`, `current_actor`, `ContextCancelled`, `log`) — all
  still exported post-subsystem-reorg. Rebase = API-drift work.
- **KNOWN BREAKAGE**: `_service.py:292` awaits
  `portal.wait_for_result()` inside
  `_open_and_supervise_service_ctx()` — that API was DELETED by
  PR #484 (`2a59cefb`). Fix: drop the call; the tuple-return
  becomes just `ctx_res` (the `Portal` "main result" notion no
  longer exists; the ctx result is the only result).
- `.uid` usages (`canceller != portal.chan.uid`) may want the
  newer `.aid.uid` spelling — check against current `Channel`.
- `log.info(f'`pikerd` service ...')` strings still say
  "pikerd" — de-piker-ify while touching.

Convergence: which `ServiceMngr` piece informs which taskman
piece,

| `hilevel_serman` | taskman |
|---|---|
| `_open_and_supervise_service_ctx()` | the parked-holder task (core primitive, factor + share) |
| `.start_service_ctx()` | `tm.run_soon()` (ctx-endpoint flavor) |
| `.start_service_task()` | (local-task variant; out of scope for taskman, stays service-only) |
| `.start_service()` | `open_worker_pool()`-ish spawn+run compose |
| `.cancel_service[_task]()` | `ctx.cancel()` / `tm.cancel()` |
| singleton `open_service_mngr()` | NOT copied — taskman is plain-scoped, no singleton |

Key semantic DELTA vs `ServiceMngr`: the service holder's
`finally: await portal.cancel_actor()` couples ACTOR lifetime
to ctx lifetime. The taskman must NOT do this — in `an=`
(cluster) placement actors are reused across tasks and their
lifetime belongs to the caller's `ActorNursery`/pool layer.
Actor-reaping stays out of the holder task entirely.

## The design

### API surface

```python
async with to_actor.open_taskman(
    # placement (pass at most one; both None -> private
    # call-scoped `open_nursery()` matching `to_actor.run()`)
    an: ActorNursery|None = None,
    portal: Portal|None = None,

    # error strategy: 'one_cancels_all' (default) | 'collect'
    strategy: str = 'one_cancels_all',
    # exit policy: 'wait' (default, trio-nursery-like) |
    # 'cancel' (service-style)
    on_exit: str = 'wait',
) as tm:

    ctx: Context = await tm.run_soon(
        fn,                  # plain async fn OR @tractor.context ep
        ptl=None,            # explicit worker override (BYO balancing)
        name=None,           # task name, default fn.__name__ (+ dedup)
        **fn_kwargs,
    )
    ...
    res = await ctx.wait_for_result()   # optional; caller-only
```

- `tm.tasks: dict[str, Context]` registry; name collision ->
  `ValueError`.
- `tm.cancel()`: graceful per-ctx `ctx.cancel()` sweep then
  holder-release; NO raw `CancelScope` exposure (the rejected
  `manage_portal() as cs` idea invites hard-cancel where
  graceful-then-escalate is the SC-blessed order).
- cluster balancing when `an=`-placed: `round_robin` default
  over `an`'s portals; `ptl=` per-call override.

### The parked-holder core (per `run_soon()`)

One holder task spawned into the tm's PRIVATE trio nursery:

```python
async def _holder(...):
    async with portal.open_context(fn, **kws) as (ctx, first):
        started_ps.started(ctx)      # hand ctx out to caller
        await release_evt.wait()     # park — NEVER on the result
    # acm exit drains/collects the ctx outcome; any error
    # raises HERE into the tm scope
```

- **discipline rule**: the caller is the SOLE result consumer
  (`ctx.wait_for_result()`); the holder never touches it. This
  avoids the two-awaiters-on-one-stream shape that plagued
  `run_in_actor()` internals (`._expect_result_ctx`).
- an un-`wait()`ed task's error still propagates: ctx exit-drain
  raises into the holder -> tm nursery -> tm scope. Nothing can
  be silently dropped.
- release triggers: task's remote completion (see impl note
  below), `ctx.cancel()`, `tm.cancel()`, tm scope exit.
- impl note: "park until done OR released" wants
  `await ctx._scope...`-ish completion OR simply parking on the
  event and letting exit-drain block on a still-running task
  per the `on_exit='wait'` policy. Start simple: park on the
  event; `on_exit='wait'` -> tm exit sets events then the acm
  exits naturally block until each remote task completes;
  `on_exit='cancel'` -> `ctx.cancel()` each live task first.

### Endpoint flavors

`run_soon()` accepts BOTH,

- `@tractor.context` endpoints -> `portal.open_context()`
  (full streaming; the holder shape above).
- plain async fns -> the `Portal.run()`-style
  `Actor.start_remote_task()` ctx (one-way protocol; no
  `ctx.started()` handshake so `first` is None-ish). Dispatch
  on the `_tractor_context_function`/`_tractor_stream_function`
  markers (see `to_actor._api._validate_one_shot_fn()` +
  `Portal.run()` for the existing dispatch precedents).

This kills the `@context`-shim friction the #477 migration
exposed (`assert_err_ctx` etc. in `test_cancellation.py`).

### Supervision strategies

- `one_cancels_all` (default): holder error propagates through
  the tm nursery -> siblings cancelled — plain trio semantics.
- `collect`: each holder catches its task's
  `RemoteActorError`, stashes it, keeps siblings running; at tm
  exit raise the lot as a `BaseExceptionGroup` (single error
  collapses per the runtime `collapse_eg()` convention). This
  resurrects the deterministic reap-all `BEG`-of-N *opt-in*:
  `test_multierror`-class assertions can re-tighten and the
  collect-don't-cancel boilerplate in
  `examples/debugging/multi_subactors.py` becomes a one-liner.
- future (NOT PR A): restart strategies (`one_for_one`...) —
  the Erlang-supervisor roadmap finally has a natural home.

### Resolved design items (w/ reasoning)

1. **exit-policy default = `wait`**: matches `trio.Nursery`
   block-exit semantics (least surprise for the "task nursery"
   framing); service-style forever-tasks opt into
   `on_exit='cancel'` (what `piker`'s `Services` effectively
   does) or use `tm.cancel()`.
2. **`collect` returns nothing extra**: errors group-raise at
   exit; VALUES stay per-ctx (`ctx.wait_for_result()`) — the tm
   carries error *policy*, not result aggregation (keeps the
   surface minimal; a gather-style values helper can wrap later).
3. **teardown ordering**: tm ctx-drain/cancel completes before
   any actor teardown *structurally* — `open_taskman(an=an)`
   nests inside the `an` block so acm exit order guarantees it;
   the holder never actor-cancels (see the ServiceMngr delta
   above).
4. **`hilevel_serman` inclusion**: rebase it as PR A's opening
   commits (it's self-contained, +618 lines, no conflicts, one
   API fix) and factor the shared supervised-ctx core so
   `ServiceMngr` + taskman diverge only in policy
   (actor-coupled + singleton vs plain-scoped). If the rebase
   fights back, fall back to pattern-mining only and leave
   `hilevel_serman` for its own PR — do NOT let it block the
   taskman.

## PR plan

- **PR A** (`to_actor/_taskman.py` MVP):
  1. rebase `hilevel_serman` -> current `main`-line (fix the
     `portal.wait_for_result()` breakage, de-piker-ify strs).
  2. factor the parked-holder core; implement `ActorTaskMngr` +
     `open_taskman()` + `run_soon()` per above.
  3. test suite `tests/test_taskman.py` mirroring
     `test_to_actor.py`'s structure: placement variants, both
     endpoint flavors, both strategies, both exit policies,
     `tm.cancel()`, error-relay + collect-BEG shapes,
     name-collision, cluster round-robin + `ptl=` override.
  4. docs: extend the one-shot sections
     (`docs/guide/{spawning,rpc}.rst`, `docs/api/core.rst`).
- **PR B**: `open_worker_pool(count=, names=, modules=)`
  (absorbs #172) + reimplement/deprecate
  `_clustering.open_actor_cluster()` atop; port
  `test_trynamic_trio` + `a_trynamic_first_scene.py` (the
  mutual-rendezvous boilerplate from `a297a32a`) +
  `multi_subactors` collect pattern + re-tighten
  `test_multierror`-class assertions; add
  `examples/parallelism/taskman_pool.py`.
- **PR C** (stretch, `piker`-side): swap
  `Services.start_service_task()` internals (and eventually the
  `open_feed()` pyramid) onto the upstream taskman.

## Verification gates (repo conventions)

- per-commit module gates on the `trio` backend + `mp_spawn`
  spot-gates; ALWAYS include `tests/test_infected_asyncio.py`
  in any gate touching spawn/supervise machinery (the #477
  lesson).
- `tests/devx/test_debugger.py` must stay byte-identical if any
  `examples/debugging/` file is touched.
- full suite (`uv run pytest tests/` w/
  `UV_PROJECT_ENVIRONMENT=py313`) green on `trio` before PR;
  NB the *full-session* `mp_spawn` run mass-fails pre-existing
  (`KeyError` in `an.cancel()`; unexercised by CI's trio-only
  matrix) — do not chase it here.
- docs `uv run --group docs make -C docs html` clean.
