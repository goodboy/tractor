---
model: openai/gpt-6-astra
service: opencode
scope: code
substantive: true
git_ref: 9547c2f9
raw_file: 20260923_ipc_fault_regressions.raw.md
---

## Human instructions

Audit terminal transport-close context interruption and reconcile the
advanced-fault suite. Preserve its original stalled-IPC cancellation
invariant using controlled cancellable waits, rather than privileged
kernel DROP tooling immediately. Write a detailed future network-outage
handoff for TIPC/WireGuard and IB resiliency. Do not disturb live setups.

## Changes

- examples/advanced_faults/ipc_failure_during_stream.py: distinguish
  detected close from stalled I/O and use explicit test registry inputs.
- tests/test_advanced_faults.py: validate transport outcomes, origin and
  child reaping instead of obsolete expected hangs/cancel acknowledgements.
- examples/advanced_faults/ipc_stalls.py and tests/test_ipc_stalls.py:
  controlled per-channel cancellable waits, actual test-root SIGINT,
   finite-deadline cleanup synchronization, and stalled-subactor-Portal
  hard-reap coverage. The example has a discoverable console entrypoint.
  The review split the transport send wrapper from its buffered
  receive-stream wrapper, moved the runnable scenario to examples,
  and extracted external process monitoring into a context manager.
- tractor/_context.py: raise unfinished recorded transport errors even
  after scope cancellation was absorbed and the channel is disconnected.
- plans/opencode/network-outage-resiliency-handoff.md: separate,
  follow-up engineering handoff for kernel/network/application
  resiliency; it is not part of the stall-test commit boundary.

## Verification

Advanced faults: 11 TCP and 11 UDS passed, including all 22 child-exit
checks. Existing EOF: 5 TCP and 5 UDS passed. Context-stream semantics:
65 TCP and 65 UDS passed. Final stall suite: 6 TCP cases passed, including
runtime hard reaping of a test-owned child whose control channel stalls.
Passing tests do not rely on forced watchdog cleanup. Reaper disabled;
legacy test registry defaults were overridden in memory for isolation.
These are working-tree checks, not commit-plan attestations.
After the review adjustments, the six stall cases passed again.
The third review retained those six cases after moving the runnable
scenario and isolating subprocess monitoring from assertions.
The next local review's six renamed TCP cases passed in 11.98 seconds
with the reaper plugin disabled; the example's CLI help also passed.

## Human contributions

The human identified the old close simulation and its intended Ctrl-C
contract, explicitly distinguished that from the newly accepted EOF
notification contract, and selected controlled runtime stalls as the
bounded first step. They supplied the Gateway/roaming incident traces
and required isolated worktrees rather than changes to live source.
The original unstaged EOF mask remains untouched in its own checkout.
During a second review, the human supplied example documentation,
asked for precise type/field references, pointed out the mixed
transport/receiver interface, and requested explicit parameter
types, method contracts, and test-mode descriptions. Those decisions
shape the final test helper and driver.
The human's third review requested the example-module location,
explicit parked-I/O documentation, code-quoted symbols and namespaces,
and a context manager for the external process monitor. They chose to
consider the longer-term network handoff in a follow-up commit.
The next local review requested explicit context/subactor Portal
distinctions, six descriptive mode names, structured case documentation
and per-branch shutdown evidence in tests/test_ipc_stalls.py and
examples/advanced_faults/ipc_stalls.py. It also requested TimeoutError
for watchdog expiry, a configurable signal with SIGINT as default,
reuse of tractor._testing.addr.get_rando_addr(), and follow-up TODOs
for a shared monitor/scenario interface and discovery daemon reuse.
The human explicitly authorized removal of the record_property TODO.

## Scope limits

No automatic IB reconnect, kernel DROP, suspend/resume monitor, native
debugger/Qt crash fix, or new production transport API is implemented.
Stall tests currently target Linux/Trio-spawner/TCP. The separate
detected-close fix was committed as `192411c4`; this stall-test
follow-up remains uncommitted.
