---
model: openai/gpt-6-astra
service: opencode
git_ref: dd758da5
diff_cmd: git diff HEAD
---

> git diff HEAD -- tractor/_context.py tractor/_exceptions.py tractor/runtime/_rpc.py tractor/devx/debug/_trace.py tractor/devx/debug/_tty_lock.py tests/test_context_status.py

Implement authoritative ContextStatus state with typed descriptor
delegation, immutable cancellation identity/origin metadata, and
status-specific formatting. Retain the human's _scope_tn rename.
Correct transport-error classification and distinguish locally
observed validation errors from boxed remote task failures.

The final working implementation passed 86 tests in 79.88 seconds
using Trio0.34.0: sixteen new reason/status tests, sixty-five context
stream semantics tests, and five IPC EOF regressions.
