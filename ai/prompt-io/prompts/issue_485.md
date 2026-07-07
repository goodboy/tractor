attempt to resolve (PR A of)
https://github.com/goodboy/tractor/issues/485
do it with /open-wkt (suggested wkt name: `taskman_485`).

REQUIRED READING before writing any code,

- the full design + impl plan (self-contained hand-off doc):
  `ai/conc-anal/to_actor_taskman_design.md`
  * it resolves the open design items, carries the
    `hilevel_serman` rebase/convergence map (incl. the known
    `portal.wait_for_result()` breakage at `_service.py:292`)
    and defines the PR A/B/C sequencing — follow it.
- the issue body (the contract + task list): #485
- prior art to derive from,
  * branch `hilevel_serman` (tip `93d161bf`): the
    `tractor/hilevel/_service.py` `ServiceMngr` port — rebase
    it first per the doc.
  * PR #363 (`oco_supervisor_prototype`): the `trionics`
    taskman prototype.
  * `ria_nursery_removal_plan.md` (same dir as the design doc)
    for the #477 background + the `open_one_shot()` sketch this
    generalizes.

house rules (also see the repo/global CLAUDE.md + skills),

- NEVER `git commit`/`push` without an explicit human ack in
  the moment; per-file/-group commits for bisectability, with
  a failing test landing BEFORE its fix (red -> green).
- gate per-commit: module tests on `trio` + `mp_spawn`
  spot-gates, ALWAYS incl. `tests/test_infected_asyncio.py`
  for spawn/supervise-touching changes; full suite green
  before PR (`UV_PROJECT_ENVIRONMENT=py313 uv run pytest`).
- all `ActorNursery` bindings are named `an:`; `trio` nursery
  bindings `tn:`.
- log prompt-io per the NLNet policy (`/prompt-io` skill) and
  update the design doc's outcome as you land steps.
