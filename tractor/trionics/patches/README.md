# `tractor.trionics.patches`

Defensive monkey-patches for bugs in `trio` itself.

## What goes here

- Bugs in upstream `trio` that we've encountered while
  running `tractor` and need to work around until
  upstream releases a fix.
- Each patch fixes EXACTLY one trio internal — no
  multi-bug omnibus patches.

## What does NOT go here

- Bugs in `tractor`'s own code (those get fixed
  in-tree, in the offending tractor module).
- Bugs in `asyncio`, `pytest`, the stdlib, etc. (file
  separate `tractor.<lib>.patches` subpkgs as
  needed).
- Workarounds for behavior we *disagree* with but that
  isn't a bug per se. If trio's API does what it says
  on the tin, we don't override it here.

## Per-patch contract

Every `_<topic>.py` module in this directory MUST
expose:

- **`apply() -> bool`** — apply the patch. Idempotent
  (safe to call multiple times). Version-gated — must
  consult `is_needed()` and skip when False. Returns
  `True` if patched this call, `False` if skipped.

- **`is_needed() -> bool`** — does upstream still need
  patching? Today most patches return `True`
  unconditionally, but as upstream releases land each
  should gate on `Version(trio.__version__) <
  Version('X.Y.Z')`. When the gated version is
  released, the patch can be DELETED entirely.

- **`repro() -> None`** — minimal demonstration of the
  bug. Used by the regression test suite to assert (a)
  the upstream bug still exists, (b) our patch fixes
  it. Should be tight enough that calling it post-
  `apply()` returns cleanly within a few hundred
  milliseconds — tests wrap it with a wall-clock cap.

Each module's docstring MUST contain:

- **Problem**: what trio does wrong + the trigger
  conditions (e.g. "fork-spawn backend, peer-closed
  socketpair, etc.")
- **Fix**: the one-line (ideally) patch
- **Repro**: the standalone snippet `repro()`
  implements
- **Upstream**: link to filed issue/PR (or
  `TODO: file`)
- **REMOVE WHEN**: `trio>=X.Y.Z` ships the upstream
  fix

## Adding a patch

1. Create `_<topic>.py` with the `apply` /
   `is_needed` / `repro` API.
2. Register it in `__init__.py::_PATCHES`.
3. Add a regression test in
   `tests/trionics/test_patches.py` that uses
   `repro()` to assert pre/post-patch behavior with a
   wall-clock cap.
4. File the upstream issue/PR. Add the link to your
   module's `Upstream:` and `# REMOVE WHEN:` lines.

## Removing a patch (when upstream releases the fix)

1. Confirm the upstream-fixed `trio` version is the
   minimum we depend on, OR keep the version-gate in
   `is_needed()` if we still support older trio.
2. If we've fully bumped past the broken versions:
   - Delete `_<topic>.py`
   - Remove the entry from `__init__.py::_PATCHES`
   - Delete the corresponding test in
     `tests/trionics/test_patches.py`
   - Bump the conc-anal doc with a "FIXED" header

## Calling

```python
from tractor.trionics.patches import apply_all
apply_all()
```

Currently invoked from `tractor._child._actor_child_main`
before `_trio_main` so every spawned subactor gets
patched. The root actor's entry could opt in too if a
patch turns out to bite the root (none do today).
