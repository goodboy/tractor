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
| `--timeout <secs>` | Override default 30s test timeout |

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

## 3. Run and report

- Run the constructed command.
- Use a timeout of **600000ms** (10min) for full suite
  runs, **120000ms** (2min) for single-file runs.
- If the suite is large (full `tests/`), consider running
  in the background and checking output when done.

### On failure:
- Show the failing test name(s) and short traceback.
- If the failure looks related to recent changes, point
  out the likely cause and suggest a fix.
- If the failure looks like a pre-existing flaky test
  (e.g. `TooSlowError`, pexpect `TIMEOUT`), note that.

### On success:
- Report the pass/fail/skip counts concisely.

## 4. Test directory layout (reference)

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

## 5. Quick-check shortcuts

For fast verification after refactors, suggest running
the "core" subset first:

```sh
python -m pytest tests/test_local.py tests/test_rpc.py tests/test_spawning.py tests/test_discovery.py -x --tb=short --no-header
```

Then expand to the full suite if those pass.
