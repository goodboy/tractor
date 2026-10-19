---
created_at: 2026-10-19T14:05:36Z
generated_by:
  harness: opencode
  provider: openai
  model: openai/gpt-6.1-sol
git_ref: 9a0f220f
diff_cmd: git diff --no-index /dev/null tests/test_context_final_return_disconnect.py
---

## Captured output

`ib_connectivity_harden` has no fix for this path. Its IB socket
disconnect handling is already in our evaluation; the two missing
commits fix idle-timer arithmetic and sampler cancellation-state
access.

I also checked Tractor’s related fault work. The existing
response-send fix covers ordinary RPCs, but leaves the `@context`
final-result send exposed. I’m building the reproducer in the
evaluated Tractor worktree, with isolated local IPC.

The reproducer hits the same failure with real loopback TCP:
**ordinary RPC passes; `@context` fails**, and the unrelated
sibling is confirmed cancelled before it can answer a request.
The two cases finish in 0.07 seconds, without IB, Qt or asyncio.

## Generated test

> `git diff --no-index /dev/null tests/test_context_final_return_disconnect.py`

The file is currently untracked. The pointer above displays the entire
generated artifact without duplicating it in this record.

finish_after_disconnect() is a real Tractor context endpoint that
publishes readiness and returns after an event. ordinary_result()
provides a non-context RPC control. The parameterized test invokes the
actual dispatcher with a real root actor and real loopback TCP channels.
It drains startup messages, closes the caller, observes callee EOF,
closes the callee channel, then releases the endpoint. A nursery sibling
must answer a subsequent request. On the observed TransportClosed it
also verifies collateral sibling cancellation and re-raises the fault.

The explicit event ordering isolates final-result delivery, without
mocked exceptions, sleeps, provider connections or process supervision.

## Final verification

Command environment: evaluation Piker py313/bin/python (Python 3.13.14,
pytest 9.0.2), importing Tractor from address_logging_levels at 9a0f220f.
VIRTUAL_ENV was removed; TRACTOR_SPAWN_METHOD and TRACTOR_LOGLEVEL unset.
LD_LIBRARY_PATH included the existing Nix GCC runtime library.

`python -m pytest tests/test_context_final_return_disconnect.py --tpt-proto=tcp --tb=short`

Result: 1 failed, 1 passed in 0.07s. Diagnostic --tb=short was used
after the ordinary full-trace run confirmed the dispatcher failure.
The context case raises TransportClosed caused by ClosedResourceError
at MsgpackTransport.send(), with the note:
The unrelated sibling was cancelled before it could answer the post-disconnect request.

`ruff check tests/test_context_final_return_disconnect.py`

All checks passed!

`git diff --check` passed. No production runtime edits were made.
