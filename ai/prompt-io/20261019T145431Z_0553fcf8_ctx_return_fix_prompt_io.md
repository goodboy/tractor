---
created_at: 2026-10-19T14:54:31Z
generated_by:
  harness: opencode
  provider: openai
  model: openai/gpt-6.1-sol
git_ref: 0553fcf8
scope: code
substantive: true
raw_file: 20261019T145431Z_0553fcf8_ctx_return_fix_prompt_io.raw.md
---

## Prompt

After committing the isolated regression on
wkt/ctx_final_return_disconnect, the human asked:

> committed, ok now a patch?

Earlier guidance requested runtime diagnostic information at the
production detection point, alongside the test's sibling metadata.

## Response summary

Added a narrow final-result delivery guard in _rpc._invoke(). A
TransportClosed while sending a successful context endpoint's final
Return now produces a metadata-rich warning rather than cancelling
the actor's shared service nursery. Actual endpoint transport errors
and cancellation retain the existing propagation path.

Extended the reproducer with a genuine endpoint-raised TransportClosed
control and an identity assertion, preventing broad suppression of
application failures. The formerly red context delivery case passes.

## Files changed

- tractor/runtime/_rpc.py: guard final response delivery and report
  actor/task/context/nursery/channel metadata on peer disconnect.
- tests/test_context_final_return_disconnect.py: endpoint-error control.
- This provenance pair.

## Verification

Ran from the Tractor worktree at committed base 0553fcf8 using
/home/goodboy/repos/piker/wkts/ib_downstream_pin_eval/py313/bin/python.
VIRTUAL_ENV was removed and LD_LIBRARY_PATH included the existing
Nix GCC runtime library; tests select isolated TCP addresses.

- pytest tests/test_context_final_return_disconnect.py tests/test_rpc.py
  --tpt-proto=tcp: 10 passed in 0.93s.
- pytest tests/test_context_stream_semantics.py --tpt-proto=tcp:
  65 passed in 59.80s.
- ruff check tractor/runtime/_rpc.py
  tests/test_context_final_return_disconnect.py: passed.
- git diff --check: passed.
- No live broker/chart trial was run by the agent.
- All patch changes remain unstaged; no commit or ref move.

## Human edits

None observed in this patch at capture time.
