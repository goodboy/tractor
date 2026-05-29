# Logging-spec leaf-module granularity — "Route B" (decouple
# logger-*identity* from console-*display*)

Follow-up notes recording the breaking-changes / costs of the
deeper fix that would give the `tractor.log` logging-spec (see
`LogSpec`/`apply_logspec()`) true **per-leaf-MODULE** level
control — deliberately *not* taken (for now) in favour of the
smaller sub-PACKAGE fix already landed.

## Status / what already shipped

The cheap, contained fix is **done**: `get_logger()`'s "strip
#2" (`log.py`, the `pkg_path = subpkg_path` collapse) no longer
eats a real sub-package component. It now strips the trailing
token *only* when it duplicates the caller's leaf-*module*
filename (which the header already shows via `{filename}`).

Result:

- `devx.debug` resolves to `tractor.devx.debug`, **distinct**
  from a bare `devx` -> `tractor.devx` (its parent). So the
  logging-spec can dial sub-package levels at any nesting depth
  (`devx.debug:runtime` ≠ `devx:cancel`).
- The `get_logger(__name__)` cosmetic ("don't repeat the leaf
  module in `{name}` since `{filename}` shows it") is preserved.

What is **still NOT addressable** after that fix:

- **Per-leaf-MODULE** levels. Every module in a (sub-)pkg shares
  that pkg's logger, because `get_logger()` drops the leaf
  module-name from the logger key by design.
- **Top-level lib modules** (eg. `tractor.to_asyncio`,
  `__package__ == 'tractor'`) emit on the *root* `tractor`
  logger, so a `to_asyncio:<lvl>` spec entry hits a phantom
  child -> no-op.

## What "Route B" is

Make the logger's *identity* the **full dotted module path**
(incl. the leaf module + top-level modules), eg.
`tractor.devx.debug._tty_lock` and `tractor.to_asyncio`, and
move the cosmetic leaf-trim out of logger-naming and into the
**formatter's `{name}` rendering**.

Net effect:

- Real per-module `Logger` nodes exist in the hierarchy ->
  the spec can target ANY module; stdlib level-inheritance and
  propagation "just work" top-down.
- Console headers stay clean because the formatter computes a
  trimmed display string (drop the trailing token that equals
  `{filename}`'s stem) instead of the logger doing it.

## Why it's "broad" — breaking changes / costs

The logger *name* is currently load-bearing well beyond
display; changing it ripples:

1. **Every logger name changes.**
   Today (post sub-pkg fix) names collapse to the sub-package;
   Route B = full module path. This touches:
   - handler attachment points + the `getChild()` hierarchy,
   - any `logging.getLogger('tractor.X')` string lookups,
   - any name-based filtering,
   - the dedup / `_strict_debug` warning logic *inside*
     `get_logger()` itself — the `pkg_name in name`,
     `leaf_mod in pkg_path`, "duplicate pkg-name" branches all
     key off the *name shape* and would need re-derivation.

2. **Formatter rewrite.**
   `LOG_FORMAT` uses `{name}` == `record.name` (the full logger
   name). To keep headers clean we must compute a *display*
   name and inject it as a record attr (eg. `record.pkg_ns`)
   via a `logging.Filter` or a `colorlog.ColoredFormatter`
   subclass overriding `.format()`, then point `LOG_FORMAT` at
   that field. The `{filename}` vs `{name}` de-dup intent has
   to be re-implemented per-record rather than per-logger.

3. **Propagation / double-emit surface grows.**
   Full-depth loggers mean more intermediate nodes
   (`...debug._tty_lock` -> `.debug` -> `.devx` -> `tractor`).
   If more than one level carries a handler (spec sub-handlers
   + a root console), records double-emit. The
   `propagate=False` trick we already use for filter-targeted
   sub-loggers (`apply_logspec()`) must be applied carefully
   across a deeper tree — more levels == more places to leak a
   dup.

4. **Level-inheritance semantics shift.**
   Today setting a level on `tractor.devx` gates *all* devx
   emits (they share that logger). Post-Route-B,
   `tractor.devx.debug._tty_lock` is its own `NOTSET` logger
   that *inherits* the effective level from ancestors —
   functionally similar via inheritance, BUT any code that does
   `log.setLevel(...)` / reads `log.level` on a (previously
   collapsed) logger now only affects that exact node. All
   `setLevel`/`.level =` call sites need an audit (eg.
   `get_logger()`'s own `log.level = rlog.level` line).

5. **Downstream contract churn.**
   `modden` / `piker` call `get_logger()` / `get_console_log()`
   and may depend on current names — including
   `modden.runtime.daemon.setup_tractor_logging()` which
   asserts `'tractor' not in name` on spec parts. The header
   `{name}` field is user-visible in everyone's logs + CI
   output. Changing the canonical names is a public-ish
   behavior change -> needs a version note + downstream
   coordination (or a formatter trim that keeps the *displayed*
   string byte-identical to today).

6. **`get_logger()` refactor risk.**
   The fn tangles two concerns: compute logger *identity* and
   compute the *display* string. Route B forces splitting them
   inside a ~300-line fn with multiple `_strict_debug`
   branches, dup-warnings, and the `name=__name__` convenience.
   High chance of subtle regressions without an exhaustive
   name-derivation test matrix.

## Migration / test plan (if pursued)

- Extract a pure helper
  `_mk_logger_name(pkg_name, mod_name, mod_pkg) -> (logger_name,
  display_name)` and cover it with an exhaustive unit matrix:
  auto vs explicit vs `__name__`; package-`__init__` vs leaf
  module; nested vs flat; `pkg_name in name` vs not; top-level
  module (`__package__ == pkg_name`).
- Switch `get_logger()` to use it for *identity*; switch the
  formatter to use `display_name` (via a record attr).
- Re-run the full suite + golden-diff a sample of rendered log
  headers to confirm zero cosmetic churn.
- Coordinate the name change with `modden`/`piker`; bump +
  CHANGES note.

## Cheaper alternative — "Route A" (record-filter)

If per-leaf control is wanted *before* committing to Route B:
keep names collapsed, add a `logging.Filter` on the configured
handler keyed on `record.module` / `record.pathname` that maps
each record's source module -> its spec level. Set the base
logger to the *minimum* level in the spec (so records aren't
pre-dropped by the logger), and let the filter discriminate
up/down within that floor.

- Pros: no name churn, no formatter change, fully contained
  next to `apply_logspec()`.
- Cons: a filter can only discriminate *within* what the logger
  admits -> base must be permissive, so `at_least_level()`
  expensive-work guards over-admit; matching dotted spec names
  to a `pathname` is fiddly; doesn't clean up the hierarchy
  itself.

## Recommendation

- Defer Route B unless true per-module loggers are wanted as a
  first-class feature.
- If per-leaf control is needed soon, prefer **Route A**
  (filter) — lower risk.
- The shipped sub-PACKAGE fix already covers the common ask
  (`devx.debug` vs `devx`).
