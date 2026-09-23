---
model: openai/gpt-6-astra
service: opencode
session: unavailable
timestamp: 2026-09-22T15:43:25Z
git_ref: 19a61e6ea14ec70b0e0f9a24d207ea8d88bd4c4c
scope: code
substantive: true
raw_file: 20260922_context_result_removal.raw.md
---

## Prompt

The human approved descriptor removal and requested Context.result
removal as a distinct small followup. Work only in the Tractor
context_fault_status worktree. First save the exact simplified status
boundary, including intended tests and provenance, using a private
index. Exclude .gitignore and .ai. Preserve the user-owned real index
containing the original descriptor-based implementation. Save tree
OIDs and binary patches under ignored .claude runtime state.

Remove only the deprecated wrapper, migrate the nine identified calls,
preserve hide_tb=False, and update stale Context.result references and
API documentation. Preserve Portal/future result APIs, maintained
notes and TODOs, and the warnings import. Verify affected suites with
the supplied interpreter and PYTHONPATH, keeping reaping disabled and
reporting fixture blockers. Downstream changes belong to the parent.

## Response summary

Saved the pre-removal status tree before changing working source, then
removed Context.result and migrated all nine calls. wait_for_result
source is byte-for-byte unchanged. Updated documentation, diagnostic
strings and Context-specific comments, including the payload message
API reference. Moved the still-relevant outcome-renaming TODO beside
wait_for_result without changing its checkbox state.

Existing public-result tests cover falsey payloads and repeated waits.
No additional mechanical API-presence regression was needed.

## Files changed

- tractor/_context.py: wrapper removal and preserved TODO relocation.
- tractor/devx/debug/_tty_lock.py: debugger context-result await.
- tests/test_inter_peer_cancellation.py: one call and references.
- tests/msg/test_pldrx_limiting.py: one call, hide_tb=False retained.
- tests/test_shm.py: one call.
- tests/test_infected_asyncio.py: one call.
- tests/test_context_stream_semantics.py: four calls and references.
- docs/api/context.rst: removal note and replacement.
- tractor/_exceptions.py, tractor/_streaming.py: comments/docstrings.
- tractor/msg/_ops.py: drain diagnostic names the supported API.
- tractor/msg/types.py: payload API reference.
- tractor/to_asyncio.py: Context API comment.
- tests/test_oob_cancellation.py: Context-specific TODO reference.
- This new provenance pair records only the separate API removal.

## Verification

All commands ran from the context_fault_status worktree, using:

```xsh
$PYTHONPATH = '/home/goodboy/repos/tractor/wkts/context_fault_status:/home/goodboy/repos/tractor/wkts/ib_disconnect_cleanup/py313/lib/python3.13/site-packages:/home/goodboy/repos/piker/py313/lib/python3.13/site-packages'
py = '/home/goodboy/repos/tractor/wkts/ib_disconnect_cleanup/py313/bin/python'
@(py) -m pytest -p no:tractor._testing._reap tests/test_context_status.py tests/test_context_stream_semantics.py tests/test_ipc_eof.py -q
@(py) -m pytest -p no:tractor._testing._reap tests/msg/test_pldrx_limiting.py -q
@(py) -m pytest -p no:tractor._testing._reap tests/test_shm.py -q
```

Results: 90, 8 and 7 passed respectively; 105 passed total.
No failures or skips in those runs. Tractor import resolved inside
this worktree. TRACTOR_SPAWN_METHOD and TRACTOR_LOGLEVEL were unset;
ambient VIRTUAL_ENV pointed to piker/py313, but the explicit interpreter
above was used throughout.

Not run: inter-peer and infected-asyncio suites require
track_orphaned_uds_per_test from the disabled reaper plugin;
infected-asyncio also requests reap_subactors_per_test. This is an
explicit coverage limitation, not a passing or skipped test result.
Debugger integration was not separately executed.

A private AST audit confirms exactly nine migrated calls and no other
executable structural changes beyond deleting the wrapper (diagnostic
strings are normalized for that comparison). An independent source
comparison confirms wait_for_result itself is unchanged. Added Python
lines fit 69 columns; git diff --check passes. Final wrapping edits
after the test runs changed only formatting, confirmed by the audit.

## Human edits

The human explicitly selected API removal as a separate followup,
identified the nine call sites and downstream ownership, constrained
the implementation to wrapper removal, and required private snapshots,
real-index preservation, fixture-blocker reporting and provenance.
These are human-directed agent edits. No direct manual source edits
were observed during this followup. Acceptance remains human-owned.
The earlier provenance pair, including its original raw record, was
preserved unchanged; its result-method retention describes the prior
status boundary, superseded only by this separate followup.
