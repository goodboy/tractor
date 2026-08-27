# Incremental Python Style Enforcement

## Objective

Enforce the mechanically checkable parts of `/py-codestyle` on new and
changed Python lines without requiring an immediate repository-wide
cleanup. Keep subjective prose guidance in review policy and expose
legacy debt separately from blocking CI.

## Current State

- `ruff.toml` sets the intended 69-column limit and enables `E501`.
- A package-and-test scan reports 1,548 existing violations.
- Ruff can enforce line length, ordinary syntax rules, unused f-string
  prefixes, and configured quote style.
- Ruff cannot directly encode several project-specific layout,
  docstring, annotation, and technical-prose policies.

The initial configuration is branch scaffolding, not a landable final
state. The implementation must make changed-code enforcement usable
before this branch targets `main`.

## Policy Matrix

Document each `/py-codestyle` rule under one enforcement class:

1. Ruff lint rule, enabled directly when the repository is clean.
2. Ruff formatter setting, used for intentionally formatted files.
3. Deterministic token or AST check, enforced on changed lines.
4. Review-only guidance where automation would produce false positives.

Start with native checks for `E501`, `F541`, quote style, import and
syntax errors. Use custom checks for exact docstring delimiters and
shape, multiline string construction, double-newline source layout,
f-string continuation consistency, local annotation placement, and
boolean-expression layout. Keep symbol-qualified technical prose and
regression-test rationale as review guidance unless a low-noise warning
can be demonstrated.

## Changed-Line Harness

Add `scripts/check_py_codestyle.py` with these responsibilities:

- Read a base revision and collect added line ranges from `git diff
  --unified=0`.
- Treat every line in an added Python file as changed.
- Run Ruff with JSON output and the incremental native-rule set.
- Report only diagnostics intersecting added line ranges.
- Run token/AST checks and apply the same changed-line filter.
- Return a nonzero status only for changed-code violations.
- Offer a separate nonblocking full-tree debt-report mode.

Keep `E501` out of the default global Ruff selection until the legacy
count reaches zero. The harness should request it explicitly so normal
repository lint remains useful during migration.

## Verification

Add focused checker tests covering:

- diff range parsing, deleted lines, renames, and new files;
- 69-column boundaries with indentation and string syntax;
- compliant and noncompliant docstring delimiters and closing shape;
- f-string continuation and double-newline source layout;
- boolean layout and local annotation placement;
- diagnostics on untouched legacy lines being ignored;
- diagnostics on newly touched legacy lines becoming blocking.

Run the checker against a fixture base and against the branch diff. Run
the existing Ruff checks without incremental rules to prove the normal
lint path remains usable.

## CI Integration

Add a dedicated CI step that derives the pull-request base commit from
the event payload and invokes the changed-line harness. Preserve a local
`--base main` mode for developers. Emit normal file, line, column, and
rule identifiers so forge annotations remain navigable.

Publish the full-tree debt count as nonblocking output. Do not hide or
silently rewrite existing source violations.

## Commit Boundaries

1. Record the 69-column policy scaffold and provenance.
2. Add the changed-line Ruff harness and its tests.
3. Add deterministic token/AST policy checks and fixtures.
4. Wire the harness into CI and document local usage.
5. Enable native rules globally only after their legacy counts reach
   zero, using separate cleanup commits where needed.

## Deferred

Type-checking migration belongs in a separate branch. It should use the
same incremental principle: establish the tool configuration and debt
inventory, gate new or changed code, publish nonblocking full-tree debt,
and tighten global enforcement as annotations and defects are repaired.
