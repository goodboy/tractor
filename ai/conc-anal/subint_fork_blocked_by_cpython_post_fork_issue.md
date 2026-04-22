# `os.fork()` from a non-main sub-interpreter aborts the child (CPython refuses post-fork cleanup)

Third `subint`-class analysis in this project. Unlike its
two siblings (`subint_sigint_starvation_issue.md`,
`subint_cancel_delivery_hang_issue.md`), this one is not a
hang — it's a **hard CPython-level refusal** of an
experimental spawn strategy we wanted to try.

## TL;DR

An in-process sub-interpreter cannot be used as a
"launchpad" for `os.fork()` on current CPython. The fork
syscall succeeds in the parent, but the forked CHILD
process is aborted immediately by CPython's post-fork
cleanup with:

```
Fatal Python error: _PyInterpreterState_DeleteExceptMain: not main interpreter
```

This is enforced by a hard `PyStatus_ERR` gate in
`Python/pystate.c`. The CPython devs acknowledge the
fragility with an in-source comment (`// Ideally we could
guarantee tstate is running main.`) but provide no
mechanism to satisfy the precondition from user code.

**Implication for tractor**: the `subint_fork` backend
sketched in `tractor.spawn._subint_fork` is structurally
dead on current CPython. The submodule is kept as
documentation of the attempt; `--spawn-backend=subint_fork`
raises `NotImplementedError` pointing here.

## Context — why we tried this

The motivation is issue #379's "Our own thoughts, ideas
for `fork()`-workaround/hacks..." section. The existing
trio-backend (`tractor.spawn._trio.trio_proc`) spawns
subactors via `trio.lowlevel.open_process()` → ultimately
`posix_spawn()` or `fork+exec`, from the parent's main
interpreter that is currently running `trio.run()`. This
brushes against a known-fragile interaction between
`trio` and `fork()` tracked in
[python-trio/trio#1614](https://github.com/python-trio/trio/issues/1614)
and siblings — mostly mitigated in `tractor`'s case only
incidentally (we `exec()` immediately post-fork).

The idea was:

1. Create a subint that has *never* imported `trio`.
2. From a worker thread in that subint, call `os.fork()`.
3. In the child, `execv()` back into
   `python -m tractor._child` — same as `trio_proc` does.
4. The fork is from a trio-free context → trio+fork
   hazards avoided regardless of downstream behavior.

The parent-side orchestration (`ipc_server.wait_for_peer`,
`SpawnSpec`, `Portal` yield) would reuse
`trio_proc`'s flow verbatim, with only the subproc-spawn
mechanics swapped.

## Symptom

Running the prototype (`tractor.spawn._subint_fork.subint_fork_proc`,
see git history prior to the stub revert) on py3.14:

```
Fatal Python error: _PyInterpreterState_DeleteExceptMain: not main interpreter
Python runtime state: initialized

Current thread 0x00007f6b71a456c0 [subint-fork-lau] (most recent call first):
  File "<script>", line 2 in <module>
<script>:2: DeprecationWarning: This process (pid=802985) is multi-threaded, use of fork() may lead to deadlocks in the child.
```

Key clues:

- The **`DeprecationWarning`** fires in the parent (before
  fork completes) — fork *is* executing, we get that far.
- The **`Fatal Python error`** comes from the child — it
  aborts during CPython's post-fork C initialization
  before any user Python runs in the child.
- The thread name `subint-fork-lau[nchpad]` is ours —
  confirms the fork is being called from the launchpad
  subint's driver thread.

## CPython source walkthrough

### Call site — `Modules/posixmodule.c:728-793`

The post-fork-child hook CPython runs in the child process:

```c
void
PyOS_AfterFork_Child(void)
{
    PyStatus status;
    _PyRuntimeState *runtime = &_PyRuntime;

    // re-creates runtime->interpreters.mutex (HEAD_UNLOCK)
    status = _PyRuntimeState_ReInitThreads(runtime);
    ...

    PyThreadState *tstate = _PyThreadState_GET();
    _Py_EnsureTstateNotNULL(tstate);

    ...

    // Ideally we could guarantee tstate is running main.   ← !!!
    _PyInterpreterState_ReinitRunningMain(tstate);

    status = _PyEval_ReInitThreads(tstate);
    ...

    status = _PyInterpreterState_DeleteExceptMain(runtime);
    if (_PyStatus_EXCEPTION(status)) {
        goto fatal_error;
    }
    ...

fatal_error:
    Py_ExitStatusException(status);
}
```

The `// Ideally we could guarantee tstate is running
main.` comment is a flashing warning sign — the CPython
devs *know* this path is fragile when fork is called from
a non-main subint, but they've chosen to abort rather than
silently corrupt state. Arguably the right call.

### The refusal — `Python/pystate.c:1035-1075`

```c
/*
 * Delete all interpreter states except the main interpreter.  If there
 * is a current interpreter state, it *must* be the main interpreter.
 */
PyStatus
_PyInterpreterState_DeleteExceptMain(_PyRuntimeState *runtime)
{
    struct pyinterpreters *interpreters = &runtime->interpreters;

    PyThreadState *tstate = _PyThreadState_Swap(runtime, NULL);
    if (tstate != NULL && tstate->interp != interpreters->main) {
        return _PyStatus_ERR("not main interpreter");       ← our error
    }

    HEAD_LOCK(runtime);
    PyInterpreterState *interp = interpreters->head;
    interpreters->head = NULL;
    while (interp != NULL) {
        if (interp == interpreters->main) {
            interpreters->main->next = NULL;
            interpreters->head = interp;
            interp = interp->next;
            continue;
        }

        // XXX Won't this fail since PyInterpreterState_Clear() requires
        // the "current" tstate to be set?
        PyInterpreterState_Clear(interp);  // XXX must activate?
        zapthreads(interp);
        ...
    }
    ...
}
```

The comment in the docstring (`If there is a current
interpreter state, it *must* be the main interpreter.`) is
the formal API contract. The `XXX` comments further in
suggest the CPython team is already aware this function
has latent issues even in the happy path.

## Chain summary

1. Our launchpad subint's driver OS-thread calls
   `os.fork()`.
2. `fork()` succeeds. Child wakes up with:
   - The parent's full memory image (including all
     subints).
   - Only the *calling* thread alive (the driver thread).
   - `_PyThreadState_GET()` on that thread returns the
     **launchpad subint's tstate**, *not* main's.
3. CPython runs `PyOS_AfterFork_Child()`.
4. It reaches `_PyInterpreterState_DeleteExceptMain()`.
5. Gate check fails: `tstate->interp != interpreters->main`.
6. `PyStatus_ERR("not main interpreter")` → `fatal_error`
   goto → `Py_ExitStatusException()` → child aborts.

Parent-side consequence: `os.fork()` in the subint
bootstrap returned successfully with the child's PID, but
the child died before connecting back. Our parent's
`ipc_server.wait_for_peer(uid)` would hang forever — the
child never gets to `_actor_child_main`.

## Definitive answer to "Open Question 1"

From the (now-stub) `subint_fork_proc` docstring:

> Does CPython allow `os.fork()` from a non-main
> sub-interpreter under the legacy config?

**No.** Not in a usable-by-user-code sense. The fork
syscall is not blocked, but the child cannot survive
CPython's post-fork initialization. This is enforced, not
accidental, and the CPython devs have acknowledged the
fragility in-source.

## What we'd need from CPython to unblock

Any one of these, from least-to-most invasive:

1. **A pre-fork hook mechanism** that lets user code (or
   tractor itself via `os.register_at_fork(before=...)`)
   swap the current tstate to main before fork runs. The
   swap would need to work across the subint→main
   boundary, which is the actual hard part —
   `_PyThreadState_Swap()` exists but is internal.

2. **A `_PyInterpreterState_DeleteExceptFor(tstate->interp)`
   variant** that cleans up all *other* subints while
   preserving the calling subint's state. Lets the child
   continue executing in the subint after fork; a
   subsequent `execv()` clears everything at the OS
   level anyway.

3. **A cleaner error** than `Fatal Python error` aborting
   the child. Even without fixing the underlying
   capability, a raised Python-level exception in the
   parent's `fork()` call (rather than a silent child
   abort) would at least make the failure mode
   debuggable.

## Upstream-report draft (for CPython issue tracker)

### Title

> `os.fork()` from a non-main sub-interpreter aborts the
> child with a fatal error in `PyOS_AfterFork_Child`; can
> we at least make it a clean `RuntimeError` in the
> parent?

### Body

> **Version**: Python 3.14.x
>
> **Summary**: Calling `os.fork()` from a thread currently
> executing inside a sub-interpreter causes the forked
> child process to abort during CPython's post-fork
> cleanup, with the following output in the child:
>
> ```
> Fatal Python error: _PyInterpreterState_DeleteExceptMain: not main interpreter
> ```
>
> From the **parent's** point of view the fork succeeded
> (returned a valid child PID). The failure is completely
> opaque to parent-side Python code — unless the parent
> does `os.waitpid()` it won't even notice the child
> died.
>
> **Root cause** (as I understand it from reading sources):
> `Modules/posixmodule.c::PyOS_AfterFork_Child()` calls
> `_PyInterpreterState_DeleteExceptMain()` with a
> precondition that `_PyThreadState_GET()->interp` be the
> main interpreter. When `fork()` is called from a thread
> executing inside a subinterpreter, the child wakes up
> with its tstate still pointing at the subint, and the
> gate in `Python/pystate.c:1044-1047` fails.
>
> A comment in the source
> (`Modules/posixmodule.c:753` — `// Ideally we could
> guarantee tstate is running main.`) suggests this is a
> known-fragile path rather than an intentional
> invariant.
>
> **Use case**: I was experimenting with using a
> sub-interpreter as a "fork launchpad" — have a subint
> that has never imported `trio`, call `os.fork()` from
> that subint's thread, and in the child `execv()` back
> into a fresh Python interpreter process. The goal was
> to sidestep known issues with `trio` + `fork()`
> interaction (see
> [python-trio/trio#1614](https://github.com/python-trio/trio/issues/1614))
> by guaranteeing the forking context had never been
> "contaminated" by trio's imports or globals. This
> approach would allow `trio`-using applications to
> combine `fork`-based subprocess spawning with
> per-worker `trio.run()` runtimes — a fairly common
> pattern that currently requires workarounds.
>
> **Request**:
>
> Ideally: make fork-from-subint work (e.g., by swapping
> the caller's tstate to main in the pre-fork hook), or
> provide a `_PyInterpreterState_DeleteExceptFor(interp)`
> variant that permits the caller's subint to survive
> post-fork so user code can subsequently `execv()`.
>
> Minimally: convert the fatal child-side abort into a
> clean `RuntimeError` (or similar) raised in the
> parent's `fork()` call. Even if the capability isn't
> expanded, the failure mode should be debuggable by
> user-code in the parent — right now it's a silent
> child death with an error message buried in the
> child's stderr that parent code can't programmatically
> see.
>
> **Related**: PEP 684 (per-interpreter GIL), PEP 734
> (`concurrent.interpreters` public API). The private
> `_interpreters` module is what I used to create the
> launchpad — behavior is the same whether using
> `_interpreters.create('legacy')` or
> `concurrent.interpreters.create()` (the latter was not
> tested but the gate is identical).
>
> Happy to contribute a minimal reproducer + test case if
> this is something the team wants to pursue.

## References

- `Modules/posixmodule.c:728` —
  [`PyOS_AfterFork_Child`](https://github.com/python/cpython/blob/main/Modules/posixmodule.c#L728)
- `Python/pystate.c:1040` —
  [`_PyInterpreterState_DeleteExceptMain`](https://github.com/python/cpython/blob/main/Python/pystate.c#L1040)
- PEP 684 (per-interpreter GIL):
  <https://peps.python.org/pep-0684/>
- PEP 734 (`concurrent.interpreters` public API):
  <https://peps.python.org/pep-0734/>
- [python-trio/trio#1614](https://github.com/python-trio/trio/issues/1614)
  — the original motivation for the launchpad idea.
- tractor issue #379 — "Our own thoughts, ideas for
  `fork()`-workaround/hacks..." section where this was
  first sketched.
- `tractor.spawn._subint_fork` — in-tree stub preserving
  the attempted impl's shape in git history.
