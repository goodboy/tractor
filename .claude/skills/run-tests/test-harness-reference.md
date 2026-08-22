# Tractor Test Harness Reference

This repository-local file supplements the canonical `/run-tests` skill.
Keep shared environment permission, process-signal safety, target selection,
failure inspection, and result reporting policy in the canonical `SKILL.md`.

## Project And Environment

- Project/import: `tractor`
- Test root: `tests/`
- Supported Python: `>=3.13,<3.15`
- Runner: pytest `>=9.0.3`
- Test dependencies: the `dev` group includes the `testing` group
- CI uses uv's default `.venv`; the Nix flake uses `py313`.
- Run from the repository root so pytest loads `pyproject.toml`.
- Do not use `default.nix` as current test-environment authority; it still
  selects unsupported Python 3.12.

Environment directory naming is not a harness invariant. Use an already
verified active project environment when available. Otherwise, use an
existing uv environment without syncing it:

```text
uv run --frozen --no-sync python -c 'import pathlib, sys, tractor; root = pathlib.Path.cwd().resolve(); mod = pathlib.Path(tractor.__file__).resolve(); print(sys.executable); print(mod); assert mod.is_relative_to(root)'
```

After module moves or collection failures, check collection with:

```text
uv run --frozen --no-sync pytest --collect-only -q tests/
```

Collection is not a mandatory precursor to every narrow run. Ask before
provisioning or changing an environment.

Before trusting CLI-selected runtime settings, inspect
`TRACTOR_SPAWN_METHOD` and `TRACTOR_LOGLEVEL`. They override the spawn method
and runtime log level passed by callers, so report active values with test
results rather than claiming the CLI flags alone selected the runtime.

## Pytest Configuration And Commands

`pyproject.toml` configures:

- `testpaths = ["tests"]` and `--rootdir=./tests`;
- importlib import mode;
- the `tractor._testing.pytest` plugin;
- xonsh plugin disablement;
- `--show-capture=no` and `--capture=fd`.

Do not silently add `-x`, `--tb=short`, or `--no-header`; those are not
project defaults. In a verified active environment, replace `uv run
--frozen --no-sync pytest` below with `python -m pytest`.

```text
# Full suite
uv run --frozen --no-sync pytest tests/

# Narrow file
uv run --frozen --no-sync pytest tests/test_local.py

# Exact node
uv run --frozen --no-sync pytest tests/discovery/test_registrar.py::test_reg_then_unreg

# Keyword selection
uv run --frozen --no-sync pytest tests/ -k 'cancel and not slow'

# Previous failures
uv run --frozen --no-sync pytest --lf
```

After verifying that the no-sync environment is current, these pytest
arguments match the Linux TCP CI row:

```text
CI=1 uv run --frozen --no-sync pytest tests/ -rsx --spawn-backend=trio --tpt-proto=tcp --capture=fd
```

## Plugin Options And Matrices

Supported spawn backends:

- `trio` (default)
- `mp_spawn`
- `mp_forkserver`

Do not advertise `subint`, `subint_forkserver`, or
`main_thread_forkserver` as runnable backends. Supported transports are
`tcp` (default) and `uds`. Run one transport per pytest session.
`mp_forkserver` and UDS are POSIX-only.

Other Tractor plugin options include:

- `--tpdb` / `--debug-mode`
- `--ll` / `--loglevel`
- `--tl` / `--tractor-loglevel`
- `--enable-stackscope`

Examples:

```text
uv run --frozen --no-sync pytest tests/ipc/ --tpt-proto=uds
uv run --frozen --no-sync pytest tests/test_spawning.py --spawn-backend=mp_spawn
uv run --frozen --no-sync pytest tests/test_spawning.py --spawn-backend=mp_forkserver --capture=sys
```

CI currently exercises Python 3.13 with the `trio` backend: TCP and UDS on
Linux and macOS, plus an informational TCP row on Windows whose pytest step
uses `continue-on-error`.

## Registry And Transport Isolation

Tests requesting the `reg_addr` fixture use addresses randomized per session:
an unreserved unprivileged loopback port for TCP or a unique socket name under
the platform runtime directory for UDS. A TCP collision remains possible.

The runtime fallback remains `127.0.0.1:1616` or `registry@1616.sock`.
Inspect that fallback only when the selected test intentionally uses runtime
defaults or a failure identifies that address. Do not perform a mandatory
`:1616` preflight or assume UDS sockets live under `/tmp`.

## Capture And Hang Diagnosis

Normal capture is `fd`. Use `--capture=sys` with `mp_forkserver`; some tests
switch to `capsys`, but the harness does not enforce that suite-wide.

For a suspected capture interaction, compare only the exact node:

```text
uv run --frozen --no-sync pytest <node> --capture=sys
uv run --frozen --no-sync pytest <node> -s
```

Treat `-s` as a diagnostic comparison, not a pass-equivalent workaround. Do
not use it to reinterpret an ordinary captured pass. Interactive `--tpdb` or
`tractor.pause()` sessions are different: they require a real TTY and disabled
capture, normally `-s`.

Do not add a global pytest timeout. `fail_after_w_trace` is Trio-cooperative;
`afk_alarm_w_trace` is a POSIX main-thread `SIGALRM` hard backstop and can
raise asynchronously. Use the latter only as a last resort, not as a generally
Trio-safe timeout replacement.

For live task-tree diagnosis:

```text
uv run --frozen --no-sync pytest <node> --enable-stackscope --capture=sys
kill -USR1 <pytest-or-subactor-pid>
```

Stackscope appends dumps to `/tmp/tractor-stackscope-<pid>.log`, including when
pytest capture hides terminal output. SIGUSR1 stackscope is unavailable on
Windows and degrades to a no-op there.

When a trace guard actually fires and snapshot capture succeeds, it writes
under `$XDG_CACHE_HOME/tractor/hung-dumps/`, falling back beneath
`~/.cache/tractor/hung-dumps/`, and prints an end-of-session index. A normal
non-timeout run creates no snapshot.

## Cleanup And `tractor-reap`

On Linux, normal pytest teardown discovers surviving descendants through
`/proc`, sends SIGINT, waits three seconds, then escalates survivors to
SIGKILL. It does not sweep shared memory and cannot run if pytest never reaches
fixture teardown.

Process discovery is a no-op off Linux. UDS PID liveness also depends on
`/proc`; on macOS, recognized PID-named sockets can therefore be classified as
dead without proof. The session-scoped autouse fixture currently passes those
candidates directly to `reap_uds()` at teardown. Do not treat its non-Linux
classification as proof of orphanhood or run concurrent live Tractor sessions
against the same UDS bindspace.

Use the CLI in inspection-only mode first:

```text
uv run --frozen --no-sync scripts/tractor-reap -n
uv run --frozen --no-sync scripts/tractor-reap --parent <pytest-pid> -n
uv run --frozen --no-sync scripts/tractor-reap --shm --uds -n
uv run --frozen --no-sync scripts/tractor-reap --uds-only -n
```

Direct `scripts/tractor-reap` execution is acceptable only after verifying its
`python3` shebang resolves the intended project environment.

Review every candidate before requesting a mutating run:

- default orphan mode is not repository-scoped;
- `--parent` trusts the supplied PID and can include non-Tractor children;
- `--shm` scans all current-user candidate files, not just Tractor-named
  files;
- `--uds` treats `registry@1616.sock` as removable even if a live default UDS
  registrar uses it.

Dry-run output prints only the initially matched root PIDs. A mutating run can
recursively expand those roots to additional descendants when `psutil` is
available. Inspect the descendant process tree separately; `-n` is not exact
signal-set parity and does not by itself authorize signaling unseen children.

The canonical skill owns signaling and unlinking authorization.

## Test Layout And Change Mapping

| Changed area | Run first |
|---|---|
| `tractor/runtime/_runtime.py`, `_state.py`, `tractor/_root.py` | `tests/test_local.py`, `tests/test_root_runtime.py`, `tests/test_runtime.py`, `tests/test_rpc.py` |
| `tractor/runtime/_portal.py`, `_rpc.py` | `tests/test_rpc.py`, `tests/test_cancellation.py` |
| `tractor/runtime/_supervise.py` | `tests/test_cancellation.py`, `tests/test_spawning.py` |
| `tractor/discovery/` | `tests/discovery/`, `tests/test_local.py` |
| `tractor/ipc/` | `tests/ipc/`, `tests/test_2way.py`, `tests/test_shm.py` as relevant |
| `tractor/spawn/` | `tests/test_spawning.py`, `tests/discovery/test_multi_program.py`, `tests/test_cancellation.py` |
| `tractor/_context.py`, `_streaming.py` | `tests/test_context_stream_semantics.py`, `tests/test_advanced_streaming.py`, `tests/test_legacy_one_way_streaming.py` |
| `tractor/to_asyncio.py` | `tests/test_infected_asyncio.py`, `tests/test_root_infect_asyncio.py` |
| `tractor/msg/` | `tests/msg/` |
| `tractor/devx/` | `tests/devx/`; debugger tests use pexpect and are comparatively slow |
| `tractor/_exceptions.py` | `tests/test_remote_exc_relay.py`, `tests/test_reg_err_types.py`, `tests/test_inter_peer_cancellation.py`, `tests/test_cancellation.py`, `tests/msg/` |

Current subdirectories include `discovery/`, `ipc/`, `msg/`, `devx/`, and
`trionics/`. There is no `tests/spawn/` directory.

## Expected Outcomes

Do not maintain a blanket known-flaky exemption list. Classify only current
explicit skip or xfail marks and exact expected signatures. Notable tracked
outcomes include:

- duplicate-name `n_dups=4` and `n_dups=8` variants in
  `tests/discovery/test_multi_program.py` are non-strict xfails;
- `tests/test_ringbuf.py` is module-skipped;
- some documentation examples have explicit macOS-CI skips.

A generic `TooSlowError` or `pexpect.TIMEOUT` is not enough to classify a
failure as pre-existing.
