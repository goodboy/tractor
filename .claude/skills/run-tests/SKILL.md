---
name: run-tests
description: >
  Run tractor test suite (or subsets). Use when the user wants
  to run tests, verify changes, or check for regressions.
argument-hint: "[test-path-or-pattern] [--opts]"
allowed-tools:
  - Bash(python -m pytest *)
  - Bash(python -c *)
  - Bash(python --version *)
  - Bash(UV_PROJECT_ENVIRONMENT=py* uv run python *)
  - Bash(UV_PROJECT_ENVIRONMENT=py* uv run pytest *)
  - Bash(UV_PROJECT_ENVIRONMENT=py* uv sync *)
  - Bash(UV_PROJECT_ENVIRONMENT=py* uv pip show *)
  - Bash(git rev-parse *)
  - Bash(ls *)
  - Bash(cat *)
  - Bash(jq * .pytest_cache/*)
  - Read
  - Grep
  - Glob
  - Task
  - AskUserQuestion
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
- `/run-tests test_registrar -v` → file + verbose
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
python -m pytest tests/discovery/test_registrar.py::test_reg_then_unreg -x -s --tpdb --ll debug

# run with UDS transport
python -m pytest tests/ -x --tb=short --no-header --tpt-proto uds

# keyword filter
python -m pytest tests/ -x --tb=short --no-header -k "cancel and not slow"
```

## 3. Pre-flight: venv detection (MANDATORY)

**Always verify a `uv` venv is active before running
`python` or `pytest`.** This project uses
`UV_PROJECT_ENVIRONMENT=py<MINOR>` naming (e.g.
`py313`) — never `.venv`.

### Step 1: detect active venv

Run this check first:

```sh
python -c "
import sys, os
venv = os.environ.get('VIRTUAL_ENV', '')
prefix = sys.prefix
print(f'VIRTUAL_ENV={venv}')
print(f'sys.prefix={prefix}')
print(f'executable={sys.executable}')
"
```

### Step 2: interpret results

**Case A — venv is active** (`VIRTUAL_ENV` is set
and points to a `py<MINOR>/` dir under the project
root or worktree):

Use bare `python` / `python -m pytest` for all
commands. This is the normal, fast path.

**Case B — no venv active** (`VIRTUAL_ENV` is empty
or `sys.prefix` points to a system Python):

Use `AskUserQuestion` to ask the user:

> "No uv venv is active. Should I activate one
> via `UV_PROJECT_ENVIRONMENT=py<MINOR> uv sync`,
> or would you prefer to activate your shell venv
> first?"

Options:
1. **"Create/sync venv"** — run
   `UV_PROJECT_ENVIRONMENT=py<MINOR> uv sync` where
   `<MINOR>` is detected from `python --version`
   (e.g. `313` for 3.13). Then use
   `py<MINOR>/bin/python` for all subsequent
   commands in this session.
2. **"I'll activate it myself"** — stop and let the
   user `source py<MINOR>/bin/activate` or similar.

**Case C — inside a git worktree** (`git rev-parse
--git-common-dir` differs from `--git-dir`):

Verify Python resolves from the **worktree's own
venv**, not the main repo's:

```sh
python -c "import tractor; print(tractor.__file__)"
```

If the path points outside the worktree, create a
worktree-local venv:

```sh
UV_PROJECT_ENVIRONMENT=py<MINOR> uv sync
```

Then use `py<MINOR>/bin/python` for all commands.

**Why this matters**: without the correct venv,
subprocesses spawned by tractor resolve modules
from the wrong editable install, causing spurious
`AttributeError` / `ModuleNotFoundError`.

### Fallback: `uv run`

If the user can't or won't activate a venv, all
`python` and `pytest` commands can be prefixed
with `UV_PROJECT_ENVIRONMENT=py<MINOR> uv run`:

```sh
# instead of: python -m pytest tests/ -x
UV_PROJECT_ENVIRONMENT=py313 uv run pytest tests/ -x

# instead of: python -c 'import tractor'
UV_PROJECT_ENVIRONMENT=py313 uv run python -c 'import tractor'
```

`uv run` auto-discovers the project and venv,
but is slower than a pre-activated venv due to
lock-file resolution on each invocation. Prefer
activating the venv when possible.

### Step 3: import + collection checks

After venv is confirmed, always run these
(especially after refactors or module moves):

```sh
# 1. package import smoke check
python -c 'import tractor; print(tractor)'

# 2. verify all tests collect (no import errors)
python -m pytest tests/ -x -q --co 2>&1 | tail -5
```

If either fails, fix the import error before running
any actual tests.

### Step 4: zombie-actor / stale-registry check (MANDATORY)

The tractor runtime's default registry address is
**`127.0.0.1:1616`** (TCP) / `/tmp/registry@1616.sock`
(UDS). Whenever any prior test run — especially one
using a fork-based backend like `subint_forkserver` —
leaks a child actor process, that zombie keeps the
registry port bound and **every subsequent test
session fails to bind**, often presenting as 50+
unrelated failures ("all tests broken"!) across
backends.

**This has to be checked before the first run AND
after any cancelled/SIGINT'd run** — signal failures
in the middle of a test can leave orphan children.

```sh
# 1. TCP registry — any listener on :1616? (primary signal)
ss -tlnp 2>/dev/null | grep ':1616' || echo 'TCP :1616 free'

# 2. leftover actor/forkserver procs — scoped to THIS
#    repo's python path, so we don't false-flag legit
#    long-running tractor-using apps (e.g. `piker`,
#    downstream projects that embed tractor).
pgrep -af "$(pwd)/py[0-9]*/bin/python.*_actor_child_main|subint-forkserv" \
  | grep -v 'grep\|pgrep' \
  || echo 'no leaked actor procs from this repo'

# 3. stale UDS registry sockets
ls -la /tmp/registry@*.sock 2>/dev/null \
  || echo 'no leaked UDS registry sockets'
```

**Interpretation:**

- **TCP :1616 free AND no stale sockets** → clean,
  proceed. The actor-procs probe is secondary — false
  positives are common (piker, any other tractor-
  embedding app); only cleanup if `:1616` is bound or
  sockets linger.
- **TCP :1616 bound OR stale sockets present** →
  surface PIDs + cmdlines to the user, offer cleanup:

  ```sh
  # 1. GRACEFUL FIRST (tractor is structured concurrent — it
  #    catches SIGINT as an OS-cancel in `_trio_main` and
  #    cascades Portal.cancel_actor via IPC to every descendant.
  #    So always try SIGINT first with a bounded timeout; only
  #    escalate to SIGKILL if graceful cleanup doesn't complete).
  pkill -INT -f "$(pwd)/py[0-9]*/bin/python.*_actor_child_main|subint-forkserv"

  # 2. bounded wait for graceful teardown (usually sub-second).
  #    Loop until the processes exit, or timeout. Keep the
  #    bound tight — hung/abrupt-killed descendants usually
  #    hang forever, so don't wait more than a few seconds.
  for i in $(seq 1 10); do
    pgrep -f "$(pwd)/py[0-9]*/bin/python.*_actor_child_main|subint-forkserv" >/dev/null || break
    sleep 0.3
  done

  # 3. ESCALATE TO SIGKILL only if graceful didn't finish.
  if pgrep -f "$(pwd)/py[0-9]*/bin/python.*_actor_child_main|subint-forkserv" >/dev/null; then
    echo 'graceful teardown timed out — escalating to SIGKILL'
    pkill -9 -f "$(pwd)/py[0-9]*/bin/python.*_actor_child_main|subint-forkserv"
  fi

  # 4. if a test zombie holds :1616 specifically and doesn't
  #    match the above pattern, find its PID the hard way:
  ss -tlnp 2>/dev/null | grep ':1616'   # prints `users:(("<name>",pid=NNNN,...))`
  # then (same SIGINT-first ladder):
  # kill -INT <NNNN>; sleep 1; kill -9 <NNNN> 2>/dev/null

  # 5. remove stale UDS sockets
  rm -f /tmp/registry@*.sock

  # 6. re-verify
  ss -tlnp 2>/dev/null | grep ':1616' || echo 'TCP :1616 now free'
  ```

**Never ignore stale registry state.** If you see the
"all tests failing" pattern — especially
`trio.TooSlowError` / connection refused / address in
use on many unrelated tests — check registry **before**
spelunking into test code. The failure signature will
be identical across backends because they're all
fighting for the same port.

**False-positive warning for step 2:** a plain
`pgrep -af '_actor_child_main'` will also match
legit long-running tractor-embedding apps (e.g.
`piker` at `~/repos/piker/py*/bin/python3 -m
tractor._child ...`). Always scope to the current
repo's python path, or only use step 1 (`:1616`) as
the authoritative signal.

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
├── discovery/           # discovery subsystem tests
│   ├── test_multiaddr.py  # multiaddr construction
│   └── test_registrar.py  # registry/discovery protocol
├── test_local.py        # registrar + local actor basics
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
| `discovery/` (`_registry`, `_discovery`, `_addr`) | `tests/discovery/ test_multi_program.py test_local.py` |
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
python -m pytest tests/test_local.py tests/test_rpc.py tests/test_spawning.py tests/discovery/test_registrar.py -x --tb=short --no-header
```

### Inspect last failures (without re-running):

When the user asks "what failed?", "show failures",
or wants to check the last-failed set before
re-running — read the pytest cache directly. This
is instant and avoids test collection overhead.

```sh
python -c "
import json, pathlib, sys
p = pathlib.Path('.pytest_cache/v/cache/lastfailed')
if not p.exists():
    print('No lastfailed cache found.'); sys.exit()
data = json.loads(p.read_text())
# filter to real test node IDs (ignore junk
# entries that can accumulate from system paths)
tests = sorted(k for k in data if k.startswith('tests/'))
if not tests:
    print('No failures recorded.')
else:
    print(f'{len(tests)} last-failed test(s):')
    for t in tests:
        print(f'  {t}')
"
```

**Why not `--cache-show` or `--co --lf`?**

- `pytest --cache-show 'cache/lastfailed'` works
  but dumps raw dict repr including junk entries
  (stale system paths that leak into the cache).
- `pytest --co --lf` actually *collects* tests which
  triggers import resolution and is slow (~0.5s+).
  Worse, when cached node IDs don't exactly match
  current parametrize IDs (e.g. param names changed
  between runs), pytest falls back to collecting
  the *entire file*, giving false positives.
- Reading the JSON directly is instant, filterable
  to `tests/`-prefixed entries, and shows exactly
  what pytest recorded — no interpretation.

**After inspecting**, re-run the failures:
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

## 9. The pytest-capture hang pattern (CHECK THIS FIRST)

**Symptom:** a tractor test hangs indefinitely under
default `pytest` but passes instantly when you add
`-s` (`--capture=no`).

**Cause:** tractor subactors (especially under fork-
based backends) inherit pytest's stdout/stderr
capture pipes via fds 1,2. Under high-volume error
logging (e.g. multi-level cancel cascade, nested
`run_in_actor` failures, anything triggering
`RemoteActorError` + `ExceptionGroup` traceback
spew), the **64KB Linux pipe buffer fills** faster
than pytest drains it. Subactor writes block → can't
finish exit → parent's `waitpid`/pidfd wait blocks →
deadlock cascades up the tree.

**Pre-existing guards in the tractor harness** that
encode this same knowledge — grep these FIRST
before spelunking:

- `tests/conftest.py:258-260` (in the `daemon`
  fixture): `# XXX: too much logging will lock up
  the subproc (smh)` — downgrades `trace`/`debug`
  loglevel to `info` to prevent the hang.
- `tests/conftest.py:316`: `# can lock up on the
  _io.BufferedReader and hang..` — noted on the
  `proc.stderr.read()` post-SIGINT.

**Debug recipe (in priority order):**

1. **Try `-s` first.** If the hang disappears with
   `pytest -s`, you've confirmed it's capture-pipe
   fill. Skip spelunking.
2. **Lower the loglevel.** Default `--ll=error` on
   this project; if you've bumped it to `debug` /
   `info`, try dropping back. Each log level
   multiplies pipe-pressure under fault cascades.
3. **If you MUST use default capture + high log
   volume**, redirect subactor stdout/stderr in the
   child prelude (e.g.
   `tractor.spawn._subint_forkserver._child_target`
   post-`_close_inherited_fds`) to `/dev/null` or a
   file.

**Signature tells you it's THIS bug (vs. a real
code hang):**

- Multi-actor test under fork-based backend
  (`subint_forkserver`, eventually `trio_proc` too
  under enough log volume).
- Multiple `RemoteActorError` / `ExceptionGroup`
  tracebacks in the error path.
- Test passes with `-s` in the 5-10s range, hangs
  past pytest-timeout (usually 30+ s) without `-s`.
- Subactor processes visible via `pgrep -af
  subint-forkserv` or similar after the hang —
  they're alive but blocked on `write()` to an
  inherited stdout fd.

**Historical reference:** this deadlock cost a
multi-session investigation (4 genuine cascade
fixes landed along the way) that only surfaced the
capture-pipe issue AFTER the deeper fixes let the
tree actually tear down enough to produce pipe-
filling log volume. Full post-mortem in
`ai/conc-anal/subint_forkserver_test_cancellation_leak_issue.md`.
Lesson codified here so future-me grep-finds the
workaround before digging.

## 10. Reaping zombie subactors (`tractor-reap`)

**Symptom:** after a `pytest` run crashes, times out,
or is `Ctrl+C`'d, subactor forks (esp. under
`subint_forkserver`) can be reparented to `init`
(PPid==1) and linger. They hold onto ports, inherit
pytest's capture-pipe fds, and flakify later
sessions.

**Two layers of defense:**

### a) Session-scoped auto-fixture (always on)

`tractor/_testing/pytest.py::_reap_orphaned_subactors`
runs at pytest session teardown. It walks `/proc` for
direct descendants of the pytest pid, SIGINTs them,
waits up to 3s, then SIGKILLs survivors. SC-polite:
gives the subactor runtime a chance to run its trio
cancel shield + IPC teardown before escalation.

This is *autouse* and session-scoped — you don't need
to do anything. It just runs.

### b) `scripts/tractor-reap` CLI (manual reap)

For the **pytest-died-mid-session** case (Ctrl+C, OOM
kill, hung process you had to `kill -9`), the fixture
never ran. Reach for the CLI:

```sh
# default: orphans (PPid==1, cwd==repo, cmd contains python)
scripts/tractor-reap

# descendant-mode: from a still-live supervisor
scripts/tractor-reap --parent <pytest-pid>

# see what would be reaped, don't signal
scripts/tractor-reap -n

# tune the SIGINT → SIGKILL grace window
scripts/tractor-reap --grace 5
```

Exit code: `0` if everyone exited on SIGINT, `1` if
SIGKILL had to escalate — so you can chain it in CI
health-checks (`scripts/tractor-reap || <alert>`).

**What it matches** (orphan-mode):
- `PPid == 1` (reparented to init → definitely
  orphaned, not just a currently-running child)
- `cwd == <repo-root>` (keeps the sweep scoped; won't
  touch unrelated init-children elsewhere)
- `python` in cmdline

**What it does not do:** kill anything whose PPid is
still a live tractor parent. If the parent is alive
it's not an orphan; use `--parent <pid>` if you need
to force-reap under a still-live supervisor.

**When NOT to run it:** while a pytest session is
active in another terminal. It's safe (won't touch
that session's live children in orphan-mode) but can
race if the target session is mid-teardown.
