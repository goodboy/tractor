---
model: openai/gpt-6-astra
service: opencode
git_ref: 0d95a9e6
diff_cmd: git diff HEAD
---

> git diff HEAD -- tractor/runtime/_rpc.py tractor/_context.py tests/test_ipc_eof.py

TransportClosed now wakes established unfinished parent contexts on
the failed channel. Delivered error/result outcomes remain intact,
including results buffered before EOF. Original disconnect causes
survive context unwinding. Tests cover idle/receiving consumers,
unrelated peers, graceful completion and cause preservation.

> git diff HEAD -- tractor/runtime/_runtime.py tests/test_cancel_soon.py

Actor.cancel_soon schedules runtime cancellation in the root nursery,
then cancels its root scope after teardown. Repeated calls do not
spawn redundant tasks. Fresh-actor tests close the service nursery,
exercise native/infected execution and verify natural lifetime
finalization unlinks all three uniquely named test-owned shm segments.

The actual IB/SIGINT/debugger sequence remains outside these focused
regressions. Runtime cancellation and idle EOF failure mechanisms were
reproduced deterministically; no installed dependencies were modified.
