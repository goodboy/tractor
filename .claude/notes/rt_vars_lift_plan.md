# `RuntimeVars` env-var lift — design plan

Status: **draft, awaiting user edits**

## Goal

Consolidate the sprawl of pytest CLI flags + ad-hoc env vars +
hardcoded fixture defaults into a *single* env-var-encoded
runtime-vars envelope, with a typed in-memory representation
(`tractor.runtime._state.RuntimeVars`) as the sole source of
truth.

## Why now

- `--tpt-proto`, `--spawn-backend`, `--diag-on-hang`,
  `--diag-capture-delay` and (soon) `TRACTOR_REG_ADDR` etc. are
  proliferating. Each adds a parsing seam.
- `tests/devx/test_debugger.py` invokes example scripts as
  separate subprocesses; they currently can't see the
  fixture-allocated `reg_addr` at all (root cause of why
  parametrizing devx scripts on `reg_addr` is on your TODO).
- Concurrent pytest sessions on the same host collide on
  shared defaults (the `registry@1616` race we just fixed is
  one symptom; per-session unique addr is the structural
  fix).
- `tractor.runtime._state.RuntimeVars: Struct` is already
  defined and **unused** — its docstring even says it
  "should be utilized as possible for future calls."

## Design

### Module: `tractor/_testing/_rtvars.py`

Lifted from `modden.runtime.env`, ~50 LOC, no new deps.

```python
_TRACTOR_RT_VARS_OSENV: str = '_TRACTOR_RT_VARS'

def dump_rtvars(rtvars: RuntimeVars|dict) -> tuple[str, str]:
    '''str-serialize via `str(dict)` — ast.literal_eval-able'''

def load_rtvars(env: dict) -> RuntimeVars:
    '''ast.literal_eval the env-var value, hydrate to struct'''

def get_rtvars(proc: psutil.Process|None = None) -> RuntimeVars:
    '''read the var from a target proc's env (or current)'''

def update_rtvars(
    rtvars: RuntimeVars|dict|None = None,
    update_osenv: bool|dict = True,
) -> tuple[str, str]:
    '''mutate + re-encode + (optionally) write to os.environ'''
```

### Encoding choice: `str(dict)` + `ast.literal_eval`

Pros:
- stdlib only
- handles all the types tractor's tests need: `str`, `int`,
  `float`, `bool`, `None`, `list`, `tuple`, `dict`
- human-readable in the env (greppable, inspectable via
  `cat /proc/<pid>/environ | tr '\0' '\n'`)

Cons:
- non-stdlib types (msgspec Structs, `Path`, custom classes)
  must be lowered first — fine for the test fixture set
- not stable across Python versions for esoteric repr cases
  (we don't hit any)

Alternatives considered:
- **msgpack**: adds a dep + binary form is ungreppable
- **json**: doesn't preserve tuples (becomes lists), which is
  a common type for `reg_addr`
- **toml/yaml**: heavier deps, no real benefit

### `RuntimeVars` becomes the single source of truth

The legacy `_runtime_vars: dict[str, Any]` global in
`runtime/_state.py` becomes a *cached view* of a
`RuntimeVars` singleton instance:

- `get_runtime_vars()` returns either the struct or a
  `.to_dict()` view depending on caller's preference
- `set_runtime_vars(...)` validates against the struct schema
- spawn-time SpawnSpec sends the struct (already does
  conceptually — just gets typed)
- `__setattr__` `breakpoint()` debug instrumentation gets
  removed (unrelated cleanup, mentioned in conversation)

### Migration path

**Phase 0** *(prep)*: strip the stray `breakpoint()` from
`RuntimeVars.__setattr__`.

**Phase 1**: land `_rtvars.py` as a leaf module, used only by
test infra. Subprocess-spawned scripts in `tests/devx/`
read `_TRACTOR_RT_VARS` on startup → reconstruct
`RuntimeVars` → call `tractor.open_root_actor(**rtvars.as_kwargs())`.
Concurrent runs become deterministic-isolated because each
session writes a unique `_registry_addrs` into the env.

**Phase 2**: migrate runtime callers (`_state.get_runtime_vars`,
spawn `SpawnSpec`, `Actor.async_main`) to operate on the
struct directly, with the dict as a compat view that gets
deprecated.

**Phase 3** *(structural)*: per-session bindspace subdir
`/run/user/<uid>/tractor/<session_uuid>/` — encoded in the
rt-vars envelope, picked up by every subactor automatically.
Obsoletes the entire bindspace-leak warning class.

## Open design questions (user input wanted)

- (placeholder for your edits)
- (placeholder)
- (placeholder)

## Out-of-scope for this lift

- Anything in `modden.runtime.env` related to `Spawn`,
  `WmCtl`, `Wks` — that's a workspace orchestration layer,
  not an env-var helper. We only lift the four utility
  functions + the var name constant.
- Switching to msgpack/json — explicitly chosen against
  above.
