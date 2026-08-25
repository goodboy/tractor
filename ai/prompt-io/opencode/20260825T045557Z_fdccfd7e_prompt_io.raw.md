---
model: gpt-5.6-sol
service: opencode
timestamp: 2026-08-25T04:55:57Z
git_ref: fdccfd7e
diff_cmd: git diff HEAD~1..HEAD
---

# Raw output - borrow existing netns bindspaces

The human reported the bindspace capability model committed and
authorized the next incremental Layer C change.

> `git diff HEAD~1..HEAD -- tractor/discovery/_bindspace.py tractor/discovery/__init__.py tests/discovery/test_bindspace.py ai/tpt-backends/03_wg_tunnel_bindspace.md`

Added async `open_existing_netns()` as a borrow-only context manager.
It opens the current process netns or a named entry under the standard
iproute2 run directory, derives stable identity from the opened FD,
and yields a borrowed process-local `BindspaceHandle`.

The context uses `O_CLOEXEC`, never creates, enters or removes a
namespace, and synchronously closes only its FD on exit. Netns keys
reject paths to keep named lookup beneath the run directory.

Added current, named, missing and traversal tests. Ruff and lock checks
passed; discovery plus message coverage passed 132 tests with 2
xpasses.
