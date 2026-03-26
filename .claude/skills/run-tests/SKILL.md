---
name: run-tests
description: >
  Run tractor test suite (or subsets). Use when the user wants
  to run tests, verify changes, or check for regressions.
argument-hint: "[test-path-or-pattern] [--opts]"
allowed-tools:
  - Bash(python -m pytest *)
  - Bash(python -c *)
  - Bash(ls *)
  - Bash(cat *)
  - Read
  - Grep
  - Glob
  - Task
---

Run the `tractor` test suite using `pytest`. Follow this
process:

## 1. Parse user intent

From the user's message and any arguments, determine:

- **scope**: full suite, specific file(s), specific
  test(s), or a keyword pattern (`-k`).
- **transport**: which IPC transport protocol to test
  against (default: `tcp`, also: `uds`).
- **options**: any extra pytest flags the user wants
  (e.g. `--ll debug`, `--tpdb`, `-x`, `-v`).

If the user provides a bare path or pattern as argument,
treat it as the test target. Examples:

- `/run-tests` → full suite
- `/run-tests test_local.py` → single file
- `/run-tests test_discovery -v` → file + verbose
- `/run-tests -k cancel` → keyword filter
- `/run-tests tests/ipc/ --tpt-proto uds` → subdir + UDS

## 2. Construct the pytest command

Base command:
```
python -m pytest
```

### Default flags (always include unless user overrides):
- `-x` (stop on first failure)
- `--tb=short` (concise tracebacks)
- `--no-header` (reduce noise)

### Path resolution:
- If the user gives a bare filename like `test_local.py`,
  resolve it under `tests/`.
- If the user gives a subdirectory like `ipc/`, resolve
  under `tests/ipc/`.
- Glob if needed: `tests/**/test_*<pattern>*.py`

### Key pytest options for this project:

| Flag | Purpose |
|---|---|
| `--ll <level>` | Set tractor log level (e.g. `debug`, `info`, `runtime`) |
| `--tpdb` / `--debug-mode` | Enable tractor's multi-proc debugger |
| `--tpt-proto <key>` | IPC transport: `tcp` (default) or `uds` |
| `--spawn-backend <be>` | Spawn method: `trio` (default), `mp_spawn`, `mp_forkserver` |
| `-k <expr>` | pytest keyword filter |
| `-v` / `-vv` | Verbosity |
| `-s` | No output capture (useful with `--tpdb`) |

### Common combos:
```sh
# quick smoke test of core modules
python -m pytest tests/test_local.py tests/test_rpc.py -x --tb=short --no-header

# full suite, stop on first failure
python -m pytest tests/ -x --tb=short --no-header

# specific test with debug
python -m pytest tests/test_discovery.py::test_reg_then_unreg -x -s --tpdb --ll debug

# run with UDS transport
python -m pytest tests/ -x --tb=short --no-header --tpt-proto uds

# keyword filter
python -m pytest tests/ -x --tb=short --no-header -k "cancel and not slow"
```

## 3. Pre-flight checks (before running tests)

### Worktree venv detection

If running inside a git worktree (`git rev-parse
--git-common-dir` differs from `--git-dir`), verify
the Python being used is from the **worktree's own
venv**, not the main repo's. Check:

```sh
python -c "import tractor; print(tractor.__file__)"
```

If the path points outside the worktree (e.g. to
the main repo), set up a local venv first:

```sh
UV_PROJECT_ENVIRONMENT=py<MINOR> uv sync
```

where `<MINOR>` matches the active cpython minor
version (detect via `python --version`, e.g.
`py313` for 3.13, `py314` for 3.14). Then use
`py<MINOR>/bin/python` for all subsequent commands.

**Why this matters**: without a worktree-local venv,
subprocesses spawned by tractor resolve modules from
the main repo's editable install, causing spurious
`AttributeError` / `ModuleNotFoundError` for code
that only exists on the worktree's branch.

### Import + collection checks

Always run these, especially after refactors or
module moves — they catch import errors instantly:

```sh
# 1. package import smoke check
python -c 'import tractor; print(tractor)'

# 2. verify all tests collect (no import errors)
python -m pytest tests/ -x -q --co 2>&1 | tail -5
```

If either fails, fix the import error before running
any actual tests.

## 4. Run and report

- Run the constructed command.
- Use a timeout of **600000ms** (10min) for full suite
  runs, **120000ms** (2min) for single-file runs.
- If the suite is large (full `tests/`), consider running
  in the background and checking output when done.
- Use `--lf` (last-failed) to re-run only previously
  failing tests when iterating on a fix.

### On failure:
- Show the failing test name(s) and short traceback.
- If the failure looks related to recent changes, point
  out the likely cause and suggest a fix.
- **Check the known-flaky list** (section 8) before
  investigating — don't waste time on pre-existing
  timeout issues.
- **NEVER auto-commit fixes.** If you apply a code fix
  during test iteration, leave it unstaged. Tell the
  user what changed and suggest they review the
  worktree state, stage files manually, and use
  `/commit-msg` (inline or in a separate session) to
  generate the commit message. The human drives all
  `git add` and `git commit` operations.

### On success:
- Report the pass/fail/skip counts concisely.

## 5. Test directory layout (reference)

```
tests/
├── conftest.py          # root fixtures, daemon, signals
├── devx/                # debugger/tooling tests
├── ipc/                 # transport protocol tests
├── msg/                 # messaging layer tests
├── test_local.py        # registrar + local actor basics
├── test_discovery.py    # registry/discovery protocol
├── test_rpc.py          # RPC error handling
├── test_spawning.py     # subprocess spawning
├── test_multi_program.py  # multi-process tree tests
├── test_cancellation.py # cancellation semantics
├── test_context_stream_semantics.py  # ctx streaming
├── test_inter_peer_cancellation.py   # peer cancel
├── test_infected_asyncio.py  # trio-in-asyncio
└── ...
```

## 6. Change-type → test mapping

After modifying specific modules, run the corresponding
test subset first for fast feedback:

| Changed module(s) | Run these tests first |
|---|---|
| `runtime/_runtime.py`, `runtime/_state.py` | `test_local.py test_rpc.py test_spawning.py test_root_runtime.py` |
| `discovery/` (`_registry`, `_discovery`, `_addr`) | `test_discovery.py test_multi_program.py test_local.py` |
| `_context.py`, `_streaming.py` | `test_context_stream_semantics.py test_advanced_streaming.py` |
| `ipc/` (`_chan`, `_server`, `_transport`) | `tests/ipc/ test_2way.py` |
| `runtime/_portal.py`, `runtime/_rpc.py` | `test_rpc.py test_cancellation.py` |
| `spawn/` (`_spawn`, `_entry`) | `test_spawning.py test_multi_program.py` |
| `devx/debug/` | `tests/devx/test_debugger.py` (slow!) |
| `to_asyncio.py` | `test_infected_asyncio.py test_root_infect_asyncio.py` |
| `msg/` | `tests/msg/` |
| `_exceptions.py` | `test_remote_exc_relay.py test_inter_peer_cancellation.py` |
| `runtime/_supervise.py` | `test_cancellation.py test_spawning.py` |

## 7. Quick-check shortcuts

### After refactors (fastest first-pass):
```sh
# import + collect check
python -c 'import tractor' && python -m pytest tests/ -x -q --co 2>&1 | tail -3

# core subset (~10s)
python -m pytest tests/test_local.py tests/test_rpc.py tests/test_spawning.py tests/test_discovery.py -x --tb=short --no-header
```

### Re-run last failures only:
```sh
python -m pytest --lf -x --tb=short --no-header
```

### Full suite in background:
When core tests pass and you want full coverage while
continuing other work, run in background:
```sh
python -m pytest tests/ -x --tb=short --no-header -q
```
(use `run_in_background=true` on the Bash tool)

## 8. Known flaky tests

These tests have **pre-existing** timing/environment
sensitivity. If they fail with `TooSlowError` or
pexpect `TIMEOUT`, they are almost certainly NOT caused
by your changes — note them and move on.

| Test | Typical error | Notes |
|---|---|---|
| `devx/test_debugger.py::test_multi_nested_subactors_error_through_nurseries` | pexpect TIMEOUT | Debugger pexpect timing |
| `test_cancellation.py::test_cancel_via_SIGINT_other_task` | TooSlowError | Signal handling race |
| `test_inter_peer_cancellation.py::test_peer_spawns_and_cancels_service_subactor` | TooSlowError | Async timing (both param variants) |
| `test_docs_examples.py::test_example[we_are_processes.py]` | `assert None == 0` | `__main__` missing `__file__` in subproc |

**Rule of thumb**: if a test fails with `TooSlowError`,
`trio.TooSlowError`, or `pexpect.TIMEOUT` and you didn't
touch the relevant code path, it's flaky — skip it.
