---
model: openai/gpt-6-astra
service: opencode
git_ref: 9547c2f9
diff_cmd: git diff HEAD
---

> git diff HEAD -- tractor/_context.py tests/test_advanced_faults.py examples/advanced_faults/ipc_failure_during_stream.py tests/test_ipc_stalls.py tests/_ipc_stalls.py

Separate terminal transport closure from unresponsive IPC in fault
coverage. Preserve original fault causes and cancellation provenance,
and test actual SIGINT only on externally controlled test-root processes.
Add normally cancellable gates with parked-operation handshakes.
An additional control-stall case requires runtime out-of-IPC hard reaping,
not cooperative cancellation through an unaffected control connection.

The corrected matrix exposed a swallowed TransportClosed after the
context nursery absorbed its own cancellation. Route that unfinished
recorded outcome through wait_for_result even on a disconnected channel.
This raises without waiting on IPC and preserves normal final outcomes.

Validation completed: 22 advanced-fault cases, 10 existing EOF cases,
130 context-stream cases, and 6 final controlled-stall cases passed.
No kernel network tools, user processes, live containers, or unrelated
shm resources were modified.
