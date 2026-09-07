---
model: openai/gpt-5.6-sol
service: opencode
session: ses_fa04e8396ffeEUnWUrYF734CSM
timestamp: 2026-09-07T01:15:33Z
git_ref: 5966e8aa
scope: code
substantive: true
raw_file: 20260907T011533Z_5966e8aa_frozen_process_local.raw.md
---

## Prompt

After landing the dependency-neutral discovery typing layer, the
maintainer approved isolating `FrozenProcessLocal` as the next clean
prerequisite before revisiting bindspace and bootstrap design.

## Response summary

Added and publicly exported the immutable process-local marker. Moved
its behavioral coverage out of dependence on bootstrap models by adding
focused test structs and regressions to the existing process-local test
module. The tests demonstrate default wire rejection, frozen struct
fields, shallow nested mutability, rejection of sentinel replacement and
`omit_defaults=True`, and guarded subclass validation hooks which cannot
replace the sentinel.

## Files changed

- `tractor/msg/_local.py` - immutable process-local marker.
- `tractor/msg/__init__.py` - public marker export.
- `tests/msg/test_process_local.py` - independent marker contract.

## Human edits

The maintainer rejected incremental salvage of the current bootstrap
planner, approved `FrozenProcessLocal` as an independent prerequisite,
and directed implementation before the bindspace re-evaluation. Existing
bootstrap review annotations and artifacts remain untouched.
