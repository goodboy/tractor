---
created_at: 2026-10-19T14:54:31Z
generated_by:
  harness: opencode
  provider: openai
  model: openai/gpt-6.1-sol
git_ref: 0553fcf8
diff_cmd: git diff HEAD -- tractor/runtime/_rpc.py tests/test_context_final_return_disconnect.py
---

## Captured response

I’ll patch the context final-result delivery path, then run the
reproducer and the related RPC/context tests.

## Generated patch

> `git diff HEAD -- tractor/runtime/_rpc.py`

Guard only chan.send(return_msg) after the context endpoint returns.
Catch normalized TransportClosed and warn with actor identity, task,
RPC/context ID, nursery, cancel scope, channel and transport error.
Keep endpoint execution and nursery-exit errors outside the guard.

> `git diff HEAD -- tests/test_context_final_return_disconnect.py`

Extend the real-TCP reproducer with an endpoint-raised TransportClosed
control. Verify its identity survives propagation and that swallowing
it would fail the test. Preserve the successful ordinary/context
cases' sibling responsiveness and RPC task cleanup assertions.

## Captured verification response

The patch turns the reproducer green. It catches
`TransportClosed` only around the final-result send and logs the
actor, task, RPC/context ID, nursery, cancel scope and channel.

An added control confirms that an endpoint’s own `TransportClosed`
still propagates with its original identity.

Checks passed: **10 RPC/reproducer cases + 65 context/stream
cases**, plus Ruff and whitespace checks.
