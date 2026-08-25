---
model: gpt-5.6-sol
service: opencode
session: tractor-addr-unpacking
timestamp: 2026-08-25T04:55:57Z
git_ref: fdccfd7e
scope: code
substantive: true
raw_file: 20260825T045557Z_fdccfd7e_prompt_io.raw.md
---

## Prompt

After committing the foundational bindspace capability model, the
human authorized the next incremental Layer C change.

## Response summary

Added a borrow-only existing-netns context manager which pins stable
identity through an open FD and always releases that local descriptor.
The API does not create, enter or remove namespaces and rejects named
path traversal.
Staged review renamed the context to `attach_netns()`, introduced the
readable `CURRENT_NETNS` alias and made key validation errors identify
their exact owning field.
Follow-up review made the empty-key error spell out that `None` is
written as `CURRENT_NETNS` for `BindspaceSpec.key` declarations.

## Files changed

- `tractor/discovery/_bindspace.py` - existing-netns lifecycle and key
  validation.
- `tractor/discovery/__init__.py` - public lifecycle export.
- `tests/discovery/test_bindspace.py` - current, named, missing and
  traversal coverage.
- `ai/tpt-backends/03_wg_tunnel_bindspace.md` - borrow-only lifecycle
  contract.

## Human edits

The human selected the previously deferred borrow-only netns lifecycle
as the next incremental Layer C change. The agent implemented the
source changes. During staged review, the human selected
`attach_netns()` terminology, requested explicit
`BindspaceSpec.key = CURRENT_NETNS` semantics and field-specific key
validation. Follow-up review requested the validation error itself
connect `None` to `CURRENT_NETNS`. The agent applied those
human-directed edits; no direct manual source edits were observed.
