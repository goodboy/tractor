---
model: openai/gpt-6-astra
service: opencode
scope: code
substantive: true
git_ref: 0d95a9e6
raw_file: 20260916_ib_disconnect_cleanup.raw.md
---

## Prompt

Fix runtime failures exposed when stopping IB Gateway: idle chart
consumers miss disconnect notification, infected actors retain closed
service nurseries, late SIGINT raises in cancel_soon, and creator shm
segments survive actor exit. Develop on piker_pin in isolation and do
not change the live piker setup.

## Response summary

Notify unfinished parent contexts on the exact failed IPC channel,
preserve delivered outcomes and original transport causes, and record
buffered Return outcomes before subsequent EOF. Schedule synchronous
actor cancellation in the root nursery rather than a possibly closed
service nursery, deduplicating repeated requests.

## Files changed

- tractor/runtime/_rpc.py: propagate transport closure to ctx owners.
- tractor/_context.py: preserve transport cause and buffered outcomes.
- tractor/runtime/_runtime.py: root-owned cancellation task.
- tests/test_ipc_eof.py: active/idle peers and graceful EOF regressions.
- tests/test_cancel_soon.py: closed-service cancellation and actual
  creator-owned shm unlinking under native/infected runtimes.

## Verification

These are session observations, not authenticated passes for the
individual commit-plan boundary trees. The shared provenance is carried
by the final cancellation boundary and describes both runtime fixes.
Exact-tree checks remain pending in the commit plan.

Baseline EOF cases: three failures and two passes. Fixed TCP selection:
34 passes; UDS EOF cases: five passes. Combined cancellation and EOF
cases: nine passes. Extended actual-SHM cancellation test: four passes;
original method injected into fresh actors: four expected failures.
No user processes or shm names were touched; no installations or
commits were made. Full Gateway/SIGINT/debugger sequence remains for
integration validation; automatic IB reconnect is not implemented.

## Human contributions

The human supplied incident traces, selected piker_pin, identified
dependent piker compatibility branches, and explicitly required worktree
isolation and preserving the live setup. Acceptance remains pending.

## Local review adjustment

The human challenged treating a locally observed transport loss like
a remote task failure/cancellation and requested readability changes.
Removed invented peer attribution from Context._canceller, added
assertions for absent cancellation provenance, restored the Return
warning, expanded predicates, and documented test ordering and fault
semantics. The proposed EOF-driven scope cancellation remains in place
only pending a separate contract decision; test success is not policy
acceptance. Piker-level explicit disconnect supervision is an alternative
to changing the runtime default. Issues #400, #368, and #22 were cited
by the human for the design discussion, not fetched in this pass.

Review verification: five EOF tests and two service-nursery cases passed.
Seven inter-peer cancellation cases could not start because disabling
automatic reaping removes their track_orphaned_uds_per_test fixture.
That coverage remains pending; no reaper was enabled against live state.
The existing advanced-fault expectations also remain to be reconciled.
