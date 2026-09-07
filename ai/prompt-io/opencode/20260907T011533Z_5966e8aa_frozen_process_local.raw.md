---
model: openai/gpt-5.6-sol
service: opencode
timestamp: 2026-09-07T01:15:33Z
git_ref: 5966e8aa
diff_cmd: git diff HEAD~1..HEAD
---

Implemented `FrozenProcessLocal` as an independent prerequisite rather
than coupling its contract to the rejected bootstrap planner.

> `git diff HEAD~1..HEAD -- tractor/msg/_local.py tractor/msg/__init__.py tests/msg/test_process_local.py`

The marker inherits `ProcessLocal` wire rejection and msgspec's frozen
field behavior. It is exported from `tractor.msg`, while focused tests
prove direct and nested encoding rejection, field immutability, and the
intentional shallow treatment of referenced mutable values.

Construction now rejects sentinel replacement and `omit_defaults=True`,
and wraps subclass validation hooks with checks before and after their
execution so they cannot skip or replace the base marker.

Verification completed with 44 process-local, WG-config, bindspace, and
tunnel-address tests passing. Ruff and scoped whitespace checks also
passed.
