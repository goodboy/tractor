# `subint` backend: abandoned-subint thread can wedge main trio event loop (Ctrl-C unresponsive)

Follow-up to the Phase B subint spawn-backend PR (see
`tractor.spawn._subint`, issue #379). The hard-kill escape
hatch we landed (`_HARD_KILL_TIMEOUT`, bounded shields,
`daemon=True` driver-thread abandonment) handles *most*
stuck-subint scenarios cleanly, but there's one class of
hang that can't be fully escaped from within tractor: a
still-running abandoned sub-interpreter can starve the
**parent's** trio event loop to the point where **SIGINT is
effectively dropped by the kernel ↔ Python boundary** —
making the pytest process un-Ctrl-C-able.

## Symptom

Running `test_stale_entry_is_deleted[subint]` under
`--spawn-backend=subint`:

1. Test spawns a subactor (`transport_fails_actor`) which
   kills its own IPC server and then
   `trio.sleep_forever()`.
2. Parent tries `Portal.cancel_actor()` → channel
   disconnected → fast return.
3. Nursery teardown triggers our `subint_proc` cancel path.
   Portal-cancel fails (dead channel),
   `_HARD_KILL_TIMEOUT` fires, driver thread is abandoned
   (`daemon=True`), `_interpreters.destroy(interp_id)`
   raises `InterpreterError` (because the subint is still
   running).
4. Test appears to hang indefinitely at the *outer*
   `async with tractor.open_nursery() as an:` exit.
5. `Ctrl-C` at the terminal does nothing. The pytest
   process is un-interruptable.

## Evidence

### `strace` on the hung pytest process

```
--- SIGINT {si_signo=SIGINT, si_code=SI_KERNEL} ---
write(37, "\2", 1) = -1 EAGAIN (Resource temporarily unavailable)
rt_sigreturn({mask=[WINCH]}) = 140585542325792
```

Translated:

- Kernel delivers `SIGINT` to pytest.
- CPython's C-level signal handler fires and tries to
  write the signal number byte (`0x02` = SIGINT) to fd 37
  — the **Python signal-wakeup fd** (set via
  `signal.set_wakeup_fd()`, which trio uses to wake its
  event loop on signals).
- Write returns `EAGAIN` — **the pipe is full**. Nothing
  is draining it.
- `rt_sigreturn` with the signal masked off — signal is
  "handled" from the kernel's perspective but the actual
  Python-level handler (and therefore trio's
  `KeyboardInterrupt` delivery) never runs.

### Stack dump (via `tractor.devx.dump_on_hang`)

At 20s into the hang, only the **main thread** is visible:

```
Thread 0x...7fdca0191780 [python] (most recent call first):
  File ".../trio/_core/_io_epoll.py", line 245 in get_events
  File ".../trio/_core/_run.py", line 2415 in run
  File ".../tests/discovery/test_registrar.py", line 575 in test_stale_entry_is_deleted
  ...
```

No driver thread shows up. The abandoned-legacy-subint
thread still exists from the OS's POV (it's still running
inside `_interpreters.exec()` driving the subint's
`trio.run()` on `trio.sleep_forever()`) but the **main
interp's faulthandler can't see threads currently executing
inside a sub-interpreter's tstate**. Concretely: the thread
is alive, holding state we can't introspect from here.

## Root cause analysis

The most consistent explanation for both observations:

1. **Legacy-config subinterpreters share the main GIL.**
   PEP 734's public `concurrent.interpreters.create()`
   defaults to `'isolated'` (per-interp GIL), but tractor
   uses `_interpreters.create('legacy')` as a workaround
   for C extensions that don't yet support PEP 684
   (notably `msgspec`, see
   [jcrist/msgspec#563](https://github.com/jcrist/msgspec/issues/563)).
   Legacy-mode subints share process-global state
   including the GIL.

2. **Our abandoned subint thread never exits.** After our
   hard-kill timeout, `driver_thread.join()` is abandoned
   via `abandon_on_cancel=True` and the thread is
   `daemon=True` so proc-exit won't block on it — but the
   thread *itself* is still alive inside
   `_interpreters.exec()`, driving a `trio.run()` that
   will never return (the subint actor is in
   `trio.sleep_forever()`).

3. **`_interpreters.destroy()` cannot force-stop a running
   subint.** It raises `InterpreterError` on any
   still-running subinterpreter; there is no public
   CPython API to force-destroy one.

4. **Shared-GIL + non-terminating subint thread → main
   trio loop starvation.** Under enough load (the subint's
   trio event loop iterating in the background, IPC-layer
   tasks still in the subint, etc.) the main trio event
   loop can fail to iterate frequently enough to drain its
   wakeup pipe. Once that pipe fills, `SIGINT` writes from
   the C signal handler return `EAGAIN` and signals are
   silently dropped — exactly what `strace` shows.

The shielded
`await actor_nursery._join_procs.wait()` at the top of
`subint_proc` (inherited unchanged from the `trio_proc`
pattern) is structurally involved too: if main trio *does*
get a schedule slice, it'd find the `subint_proc` task
parked on `_join_procs` under shield — which traps whatever
`Cancelled` arrives. But that's a second-order effect; the
signal-pipe-full condition is the primary "Ctrl-C doesn't
work" cause.

## Why we can't fix this from inside tractor

- **No force-destroy API.** CPython provides neither a
  `_interpreters.force_destroy()` nor a thread-
  cancellation primitive (`pthread_cancel` is actively
  discouraged and unavailable on Windows). A subint stuck
  in pure-Python loops (or worse, C code that doesn't poll
  for signals) is structurally unreachable from outside.
- **Shared GIL is the root scheduling issue.** As long as
  we're forced into legacy-mode subints for `msgspec`
  compatibility, the abandoned-thread scenario is
  fundamentally a process-global GIL-starvation window.
- **`signal.set_wakeup_fd()` is process-global.** Even if
  we wanted to put our own drainer on the wakeup pipe,
  only one party owns it at a time.

## Current workaround

- **Fixture-side SIGINT loop on the `daemon` subproc** (in
  this test's `daemon: subprocess.Popen` fixture in
  `tests/conftest.py`). The daemon dying closes its end of
  the registry IPC, which unblocks a pending recv in main
  trio's IPC-server task, which lets the event loop
  iterate, which drains the wakeup pipe, which finally
  delivers the test-harness SIGINT.
- **Module-level skip on py3.13**
  (`pytest.importorskip('concurrent.interpreters')`) — the
  private `_interpreters` C module exists on 3.13 but the
  multi-trio-task interaction hangs silently there
  independently of this issue.

## Path forward

1. **Primary**: upstream `msgspec` PEP 684 adoption
   ([jcrist/msgspec#563](https://github.com/jcrist/msgspec/issues/563)).
   Unlocks `concurrent.interpreters.create()` isolated
   mode → per-interp GIL → abandoned subint threads no
   longer starve the parent's main trio loop. At that
   point we can flip `_subint.py` back to the public API
   (`create()` / `Interpreter.exec()` / `Interpreter.close()`)
   and drop the private `_interpreters` path.

2. **Secondary**: watch CPython for a public
   force-destroy primitive. If something like
   `Interpreter.close(force=True)` lands, we can use it as
   a hard-kill final stage and actually tear down
   abandoned subints.

3. **Harness-level**: document the fixture-side SIGINT
   loop pattern as the "known workaround" for subint-
   backend tests that can leave background state holding
   the main event loop hostage.

## References

- PEP 734 (`concurrent.interpreters`):
  <https://peps.python.org/pep-0734/>
- PEP 684 (per-interpreter GIL):
  <https://peps.python.org/pep-0684/>
- `msgspec` PEP 684 tracker:
  <https://github.com/jcrist/msgspec/issues/563>
- CPython `_interpretersmodule.c` source:
  <https://github.com/python/cpython/blob/main/Modules/_interpretersmodule.c>
- `tractor.spawn._subint` module docstring (in-tree
  explanation of the legacy-mode choice and its
  tradeoffs).

## Reproducer

```
./py314/bin/python -m pytest \
  tests/discovery/test_registrar.py::test_stale_entry_is_deleted \
  --spawn-backend=subint \
  --tb=short --no-header -v
```

Hangs indefinitely without the fixture-side SIGINT loop;
with the loop, the test completes (albeit with the
abandoned-thread warning in logs).

## Additional known-hanging tests (same class)

All three tests below exhibit the same
signal-wakeup-fd-starvation fingerprint (`write() → EAGAIN`
on the wakeup pipe after enough SIGINT attempts) and
share the same structural cause — abandoned legacy-subint
driver threads contending with the main interpreter for
the shared GIL until the main trio loop can no longer
drain its wakeup pipe fast enough to deliver signals.

They're listed separately because each exposes the class
under a different load pattern worth documenting.

### `tests/discovery/test_registrar.py::test_stale_entry_is_deleted[subint]`

Original exemplar — see the **Symptom** and **Evidence**
sections above. One abandoned subint
(`transport_fails_actor`, stuck in `trio.sleep_forever()`
after self-cancelling its IPC server) is sufficient to
tip main into starvation once the harness's `daemon`
fixture subproc keeps its half of the registry IPC alive.

### `tests/test_cancellation.py::test_cancel_while_childs_child_in_sync_sleep[subint-False]`

Cancel a grandchild that's in sync Python sleep from 2
nurseries up. The test's own docstring declares the
dependency: "its parent should issue a 'zombie reaper' to
hard kill it after sufficient timeout" — which for
`trio`/`mp_*` is an OS-level `SIGKILL` of the grandchild
subproc. **Under `subint` there's no equivalent** (no
public CPython API to force-destroy a running
sub-interpreter), so the grandchild's sync-sleeping
`trio.run()` persists inside its abandoned driver thread
indefinitely. The nested actor-tree (parent → child →
grandchild, all subints) means a single cancel triggers
multiple concurrent hard-kill abandonments, each leaving
a live driver thread.

This test often only manifests the starvation under
**full-suite runs** rather than solo execution —
earlier-in-session subint tests also leave abandoned
driver threads behind, and the combined population is
what actually tips main trio into starvation. Solo runs
may stay Ctrl-C-able with fewer abandoned threads in the
mix.

### `tests/test_cancellation.py::test_multierror_fast_nursery[subint-25-0.5]`

Nursery-error-path throughput stress-test parametrized
for **25 concurrent subactors**. When the multierror
fires and the nursery cancels, every subactor goes
through our `subint_proc` teardown. The bounded
hard-kills run in parallel (all `subint_proc` tasks are
sibling trio tasks), so the timeout budget is ~3s total
rather than 3s × 25. After that, **25 abandoned
`daemon=True` driver threads are simultaneously alive** —
an extreme pressure multiplier on the same mechanism.

The `strace` fingerprint is striking under this load: six
or more **successful** `write(16, "\2", 1) = 1` calls
(main trio getting brief GIL slices, each long enough to
drain exactly one wakeup-pipe byte) before finally
saturating with `EAGAIN`:

```
--- SIGINT {si_signo=SIGINT, si_code=SI_KERNEL} ---
write(16, "\2", 1)                      = 1
rt_sigreturn({mask=[WINCH]})            = 140141623162400
--- SIGINT {si_signo=SIGINT, si_code=SI_KERNEL} ---
write(16, "\2", 1)                      = 1
rt_sigreturn({mask=[WINCH]})            = 140141623162400
--- SIGINT {si_signo=SIGINT, si_code=SI_KERNEL} ---
write(16, "\2", 1)                      = 1
rt_sigreturn({mask=[WINCH]})            = 140141623162400
--- SIGINT {si_signo=SIGINT, si_code=SI_KERNEL} ---
write(16, "\2", 1)                      = 1
rt_sigreturn({mask=[WINCH]})            = 140141623162400
--- SIGINT {si_signo=SIGINT, si_code=SI_KERNEL} ---
write(16, "\2", 1)                      = 1
rt_sigreturn({mask=[WINCH]})            = 140141623162400
--- SIGINT {si_signo=SIGINT, si_code=SI_KERNEL} ---
write(16, "\2", 1)                      = 1
rt_sigreturn({mask=[WINCH]})            = 140141623162400
--- SIGINT {si_signo=SIGINT, si_code=SI_KERNEL} ---
write(16, "\2", 1)                      = -1 EAGAIN (Resource temporarily unavailable)
rt_sigreturn({mask=[WINCH]})            = 140141623162400
```

Those successful writes indicate CPython's
`sys.getswitchinterval()`-based GIL round-robin *is*
giving main brief slices — just never long enough to run
the Python-level signal handler through to the point
where trio converts the delivered SIGINT into a
`Cancelled` on the appropriate scope. Once the
accumulated write rate outpaces main's drain rate, the
pipe saturates and subsequent signals are silently
dropped.

The `pstree` below (pid `530060` = hung `pytest`) shows
the subint-driver thread population at the moment of
capture. Even with fewer than the full 25 shown (pstree
truncates thread names to `subint-driver[<interp_id>` —
interpreters `3` and `4` visible across 16 thread
entries), the GIL-contender count is more than enough to
explain the starvation:

```
>>> pstree -snapt 530060
systemd,1 --switched-root --system --deserialize=40
  └─login,1545 --
      └─bash,1872
          └─sway,2012
              └─alacritty,70471 -e xonsh
                  └─xonsh,70487 .../bin/xonsh
                      └─uv,70955 run xonsh
                          └─xonsh,70959 .../py314/bin/xonsh
                              └─python,530060 .../py314/bin/pytest -v tests/test_cancellation.py --spawn-backend=subint
                                  ├─{subint-driver[3},531857
                                  ├─{subint-driver[3},531860
                                  ├─{subint-driver[3},531862
                                  ├─{subint-driver[3},531866
                                  ├─{subint-driver[3},531877
                                  ├─{subint-driver[3},531882
                                  ├─{subint-driver[3},531884
                                  ├─{subint-driver[3},531945
                                  ├─{subint-driver[3},531950
                                  ├─{subint-driver[3},531952
                                  ├─{subint-driver[4},531956
                                  ├─{subint-driver[4},531959
                                  ├─{subint-driver[4},531961
                                  ├─{subint-driver[4},531965
                                  ├─{subint-driver[4},531968
                                  └─{subint-driver[4},531979
```

(`pstree` uses `{...}` to denote threads rather than
processes — these are all the **driver OS-threads** our
`subint_proc` creates with name
`f'subint-driver[{interp_id}]'`. Every one of them is
still alive, executing `_interpreters.exec()` inside a
sub-interpreter our hard-kill has abandoned. At 16+
abandoned driver threads competing for the main GIL, the
main-interpreter trio loop gets starved and signal
delivery stalls.)
