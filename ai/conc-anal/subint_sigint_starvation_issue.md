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
