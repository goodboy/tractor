---
created_at: 2026-10-21T14:53:44Z
generated_by:
  harness: opencode
  provider: openai
  model: openai/gpt-6.1-sol
git_ref: 0553fcf8
scope: code
substantive: true
raw_file: 20261021T145344Z_0553fcf8_task_format_prompt_io.raw.md
---

## Prompt

The human requested moving the new regression-local Trio task formatter
into devx.pformat, scanning branches for earlier equivalents first, and
noted that log.pformat_task_uid already extracts the task ID. Follow-up:
the reviewed change was good enough apart from the function move; the
QA agent had already been alerted to the new order-bootstrap race.

## Response summary

Found no earlier detailed task formatter across 810 local refs and 18
worktrees. Extracted pformat_task into tooling and made the existing UID
helper accept an explicit task, preserving its established log output.
Added focused coverage that inspects a real child from its parent and
updated the cancellation regression to use the shared renderer.

## Files changed

- `tractor/devx/pformat.py`: reusable pformat_task.
- `tractor/log.py`: optional explicit task for pformat_task_uid.
- `tests/devx/test_pformat.py`: real task-selection/ownership coverage.
- `tests/test_context_final_return_disconnect.py`: shared-tool callers.

## Review boundary and verification

Pre-move tree: `27e75d8b1cdfec1d96284d993e55ad3c51c3804c`.
HEAD remains `0553fcf88cd37b4d487eafb992071d4d1cc6e06f`.
The real index was unchanged at extraction start. Existing final-send
work and provenance are retained, including the earlier review record
that documents the then-local helper. Original Tuicr replies were not
rewritten; this extraction supersedes their local-helper description.
See the raw partner for environment details and focused verification.

Final verification repeated after import/line-wrap adjustments: 75
formatter/logger tests and 10 final-Return/RPC tests passed; Ruff,
whitespace and added-source 69-column checks passed. A read-only Tuicr
comparison found the saved session now contains only the first original
comment/reply pair, unlike the earlier three-pair receipt. This extraction
did not write session state or recreate the absent comments.

## Human edits

This extraction follows the human's requested final placement. Existing
logging TODOs, pre-review changes and task/review states are preserved.
