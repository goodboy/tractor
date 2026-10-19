---
created_at: 2026-10-19T14:05:36Z
generated_by:
  harness: opencode
  provider: openai
  model: openai/gpt-6.1-sol
git_ref: 9a0f220f
scope: tests
substantive: true
raw_file: 20261019T140536Z_9a0f220f_final_return_prompt_io.raw.md
---

## Prompt

The human reported chart Ctrl-C causing search's final Return delivery
to raise TransportClosed in datad.ib, cancelling data services and then
pikerd's trading/sampler services. They asked whether the Piker branch
ib_connectivity_harden already remediates it and authorized a minimal
Tractor sibling-survival reproducer if no existing remediation applies:

> ok let's do it if we find no other redmediations per above.

The human explicitly selected capture of this test-only provenance.

## Response summary

Compared ib_connectivity_harden with the committed evaluation baseline:
efa72980's IB socket-disconnect propagation is already present; the
missing d126302f/c0dae89a changes concern idle arithmetic and sampler
cancellation-state access. Tractor ebf2258b hardens ordinary RPC response
sends, but the context final Return remains exposed. Inspected the
additional ipc_fault_regressions branch: its EOF/context-status changes
do not guard this final-result send.

Added a focused real-TCP, same-process dispatcher regression with an
ordinary RPC control and a context case. Explicit peer-close/EOF events
force the failure ordering. A sibling request/response checks service
survival; the failure path confirms premature sibling cancellation.

## Files changed

- tests/test_context_final_return_disconnect.py: reproducer and control.
- This provenance pair.

## Verification

- Ordinary RPC control passed.
- Context case failed with the reported TransportClosed caused by
  ClosedResourceError, confirming collateral nursery cancellation.
- Final two-case run: 1 failed, 1 passed in 0.07s.
- Ruff passed; git diff --check passed.
- Test uses isolated local TCP, no broker, feed, Qt or asyncio bridge.
- Root actor and channels exit through their context managers.
- No production fix, staging, commit or ref changes.

## Human edits

None observed in the generated test at capture time.

## Follow-up: identify the collateral sibling

The human requested the task repr and runtime metadata behind the
test-generated exception note. The cancellation handler now captures
the sibling task, Actor.aid (including PID), parent nursery, nursery
cancel scope and original trio.Cancelled repr while still unwinding.
The test attaches that snapshot to the escaping TransportClosed;
Tractor production reporting and exception handling are unchanged.

The updated two-case reproducer again yielded 1 passed, 1 failed in
0.07s, with the sibling identity and source invoke task in the note.
Ruff and git diff --check passed after the diagnostic change.
