# `test_register_duplicate_name` racy connect-failure on `daemon` fixture readiness

## Symptom

`tests/test_multi_program.py::test_register_duplicate_name`
fails intermittently under BOTH transports + ALL spawn
backends with connect-refused errors:

```
# under --tpt-proto=uds
FAILED tests/test_multi_program.py::test_register_duplicate_name
- ConnectionRefusedError: [Errno 111] Connection refused
( ^^^ this exc was collapsed from a group ^^^ )

# under --tpt-proto=tcp
FAILED tests/test_multi_program.py::test_register_duplicate_name
- OSError: all attempts to connect to 127.0.0.1:36003 failed
( ^^^ this exc was collapsed from a group ^^^ )
```

Distinct from the cancel-cascade `TooSlowError` flake
class — see
`cancel_cascade_too_slow_under_main_thread_forkserver_issue.md`.
This is a **connect-time race** before the daemon is
fully ready to `accept()`, not a teardown-cascade
slowness.

## Root cause: blind `time.sleep()` in `daemon` fixture

`tests/conftest.py::daemon` boots a sub-py-process via
`subprocess.Popen([python, '-c', 'tractor.run_daemon(...)'])`,
then **blindly sleeps** a fixed delay before yielding
`proc` to the test:

```python
# excerpt from tests/conftest.py::daemon
proc = subprocess.Popen([
    sys.executable, '-c', code,
])

bg_daemon_spawn_delay: float = _PROC_SPAWN_WAIT  # 0.6
if tpt_proto == 'uds':
    bg_daemon_spawn_delay += 1.6
if _non_linux and ci_env:
    bg_daemon_spawn_delay += 1

# XXX, allow time for the sub-py-proc to boot up.
# !TODO, see ping-polling ideas above!
time.sleep(bg_daemon_spawn_delay)

assert not proc.returncode
yield proc
```

Inherent fragility: the delay is "long enough on dev
boxes most of the time" but has no actual
synchronization with the daemon's `bind()` + `listen()`
completion. Under any of:

- Loaded box (CI parallelism, big rebuild in
  background, low-cpu-freq)
- Cold first-run (`importlib` cache miss, JIT warmup)
- Higher-than-expected `tractor` import cost
- Filesystem latency (UDS sockfile create, slow
  tmpfs)

...the sleep finishes BEFORE the daemon has bound its
listen socket → first test client call to
`tractor.find_actor()` / `wait_for_actor()` /
`open_nursery(registry_addrs=[reg_addr])`'s implicit
connect → `ConnectionRefusedError` (TCP) or
`FileNotFoundError`/`ConnectionRefusedError` (UDS).

## Reproducer

Easiest: run the suite under load.

```bash
# create CPU pressure on another core in parallel
stress-ng --cpu 2 --timeout 600s &

./py313/bin/python -m pytest \
  tests/test_multi_program.py::test_register_duplicate_name \
  --spawn-backend=main_thread_forkserver \
  --tpt-proto=tcp -v
```

Reproduces ~30-50% of the time on a dev laptop. On a
quiet idle box, may need 5-10 runs to hit.

## Why the existing `_PROC_SPAWN_WAIT` tuning is
inadequate

Recent `bg_daemon_spawn_delay` rename
(de-monotonic-grow fix) just-shipped removed the
*accumulation* bug where each invocation made the
NEXT test's wait longer too. Net effect: every
invocation now uses the SAME `0.6 + 1.6` (UDS) or
`0.6` (TCP) sleep, no growth. Good — but does
NOTHING for the underlying race. Each individual
test still relies on a blind sleep that may or may
not be sufficient.

Bumping the constant higher pushes flake rate down
but never to zero AND adds dead time to every
non-flaking run. Not a fix, just a knob.

## Side effects

- **Inter-test cascade**: a single failure can cascade
  via leaked subprocesses (the `daemon` fixture's
  cleanup may not fully tear down a daemon that never
  reached "ready"). The `_reap_orphaned_subactors`
  session-end + `_track_orphaned_uds_per_test`
  per-test fixtures handle most of this now, but the
  affected test itself still fails.
- **Worsens under fork-spawn backends**: the daemon
  has more init work
  (`_main_thread_forkserver`-coordinator-thread
  startup, etc.) so the sleep has to cover MORE.

## Fix design — replace blind sleep with active poll

The right primitive is **poll the daemon's bind
address until it accepts a connection or we time
out**, with the timeout being a hard ceiling rather
than a baseline. Two implementation paths:

### Path A — TCP/UDS connect-poll loop

Try `socket.connect(reg_addr)` in a tight loop with
short backoff (~50ms), succeed on the first non-error
return, fail-loud on a hard cap (e.g. 10s). Same
primitive works for both transports because both use
`socket.connect()` semantics.

Rough shape:

```python
def _wait_for_daemon_ready(
    reg_addr,
    tpt_proto: str,
    timeout: float = 10.0,
    poll_interval: float = 0.05,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if tpt_proto == 'tcp':
            sock = socket.socket(socket.AF_INET)
            target = reg_addr  # (host, port)
        else:  # uds
            sock = socket.socket(socket.AF_UNIX)
            target = os.path.join(*reg_addr)
        try:
            sock.settimeout(poll_interval)
            sock.connect(target)
        except (
            ConnectionRefusedError,
            FileNotFoundError,
            socket.timeout,
        ) as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f'Daemon never accepted on {target!r} '
                    f'within {timeout}s'
                ) from exc
            time.sleep(poll_interval)
        else:
            sock.close()
            return
```

Pros: trivial primitive, no tractor-runtime
dependency, works pre-yield in the fixture body,
fail-fast on truly-broken daemon.
Cons: doesn't actually do an IPC handshake, just
proves listen-side is up. A daemon that bound but
hasn't initialized its registrar table yet would
still race.

### Path B — `tractor.find_actor()` poll

Use the actual discovery API the test would call:

```python
async def _wait_for_daemon_ready_via_discovery(
    reg_addr,
    timeout: float = 10.0,
    poll_interval: float = 0.05,
):
    deadline = trio.current_time() + timeout
    async with tractor.open_root_actor(
        registry_addrs=[reg_addr],
        # ephemeral root just for the probe
    ):
        while True:
            try:
                async with tractor.find_actor(
                    'registrar',  # daemon's own name
                    registry_addrs=[reg_addr],
                ) as portal:
                    if portal is not None:
                        return
            except Exception:
                pass
            if trio.current_time() >= deadline:
                raise TimeoutError(...)
            await trio.sleep(poll_interval)
```

Pros: actually proves the discovery path works,
handles the "bound but not ready" case naturally.
Cons: requires booting an ephemeral root actor JUST
for the probe (overhead), more code, and runs in trio
which complicates the sync-fixture context. Need a
`trio.run()` wrapper.

### Recommended: Path A with optional handshake check

Path A is much simpler + handles 95% of the bug
class. If "bound-but-not-ready" turns out to still
race (it shouldn't — `tractor.run_daemon` doesn't
return from `bind()` until the registrar is
fully populated), escalate to Path B as a focused
follow-up.

## Workarounds (until fix lands)

1. **Bump `_PROC_SPAWN_WAIT`** higher (current: 0.6).
   2.0–3.0 hides most flakes at the cost of adding
   dead time to every test. Not a fix but reduces
   blast radius while the proper poll lands.
2. **`pytest-rerunfailures`** with `reruns=1` on the
   `daemon` fixture's tests specifically. Hides the
   flake but doesn't address it.
3. **Mark known-affected tests as `xfail(strict=False)`**
   under `--ci`. Lets CI go green at the cost of
   silently hiding regressions.

(Recommend skipping all three — implement the active
poll instead.)

## Investigation next steps

1. Implement Path A as a `_wait_for_daemon_ready()`
   helper in `tests/conftest.py`. Replace the
   `time.sleep(bg_daemon_spawn_delay)` call with it.
2. Drop the `_PROC_SPAWN_WAIT` constant entirely
   (active poll obsoletes blind sleep).
3. Run the suite 5-10 times to validate flake rate
   drops to 0.
4. If flakes persist, profile whether the daemon
   process exits with non-zero before the poll's
   deadline hits — that'd be a different bug
   (daemon startup crash) that the blind sleep was
   masking.
5. Cross-check `tests/test_multi_program.py::test_*`
   — multiple tests use the `daemon` fixture; all
   should benefit from the same poll primitive.

## Related

- `tests/conftest.py::daemon` — the fixture under
  fix
- `tests/conftest.py::_PROC_SPAWN_WAIT` — the
  constant to drop
- `cancel_cascade_too_slow_under_main_thread_forkserver_issue.md`
  — distinct flake class (cancel-cascade
  `TooSlowError` at teardown, not connect-time race)
- `trio_wakeup_socketpair_busy_loop_under_fork_issue.md`
  — different bug entirely; this race was masked
  pre-WakeupSocketpair-patch by the busy-loop
  hangs.
