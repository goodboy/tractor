---
model: openai/gpt-6-astra
service: opencode
timestamp: 2026-09-22T15:43:25Z
git_ref: 19a61e6ea14ec70b0e0f9a24d207ea8d88bd4c4c
diff_cmd: git diff HEAD~1..HEAD
---

## Generated response

Remove the deprecated Context.result wrapper as a separate followup
to the simplified ContextStatus migration. Migrate nine calls across
six files to wait_for_result, retaining hide_tb=False. Document the
removal and update Context-specific comments and diagnostic strings.
Keep the outcome-renaming TODO with wait_for_result.

> `git diff HEAD~1..HEAD -- tractor/_context.py`

Delete only the wrapper; wait_for_result source is byte-for-byte
unchanged. Keep the warnings import used by send_yield.

> `git diff HEAD~1..HEAD -- tractor/devx/debug/_tty_lock.py`
> `git diff HEAD~1..HEAD -- tests/test_inter_peer_cancellation.py`
> `git diff HEAD~1..HEAD -- tests/msg/test_pldrx_limiting.py`
> `git diff HEAD~1..HEAD -- tests/test_shm.py`
> `git diff HEAD~1..HEAD -- tests/test_infected_asyncio.py`
> `git diff HEAD~1..HEAD -- tests/test_context_stream_semantics.py`

Migrate the debugger lock call and eight test calls, including four
context-stream calls. Preserve arguments and surrounding behavior.

> `git diff HEAD~1..HEAD -- docs/api/context.rst`
> `git diff HEAD~1..HEAD -- tractor/_exceptions.py`
> `git diff HEAD~1..HEAD -- tractor/_streaming.py`
> `git diff HEAD~1..HEAD -- tractor/msg/_ops.py`
> `git diff HEAD~1..HEAD -- tractor/msg/types.py`
> `git diff HEAD~1..HEAD -- tractor/to_asyncio.py`
> `git diff HEAD~1..HEAD -- tests/test_oob_cancellation.py`

Replace stale Context.result references with the supported method.
The API page explicitly records removal and its replacement.

## Verification output

Context-status, context-stream and IPC EOF: 90 passed in 76.04s.
Payload limiting, separate process: 8 passed in 3.64s.
Shared memory, separate process: 7 passed in 5.17s.
Total: 105 passed, no failures or skips in these runs.

Inter-peer and infected-asyncio suites were not run: their module
fixture requirements depend on the disabled reaper plugin. No cleanup
plugin was enabled. Debugger integration was not separately executed.

9 call migrations; wait_for_result source unchanged; AST OK
All added Python lines fit 69 columns

git diff --check passed. Existing public-result tests already cover
falsey outcomes and repeated waits; no new presence-only test was added.
Original staged index SHA-256 remained unchanged through verification.

## Diff boundary

The eventual HEAD~1..HEAD pointers assume this pair accompanies only
the API-removal followup. Before committing, use the private snapshot
patches under .claude/wkts/context-status-boundary instead. The
pre-removal tree is 94858f83875d9809b67d517f2b0bc52ccbd09ded.
