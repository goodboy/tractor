# Revisit `subint_forkserver` thread-cache constraints once msgspec PEP 684 support lands

> **Tracked at:** [#450](https://github.com/goodboy/tractor/issues/450)

Follow-up tracker for cleanup work gated on the msgspec
PEP 684 adoption upstream ([jcrist/msgspec#563](https://github.com/jcrist/msgspec/issues/563)).

Context — why this exists
-------------------------

The `tractor.spawn._subint_forkserver` submodule currently
carries two "non-trio" thread-hygiene constraints whose
necessity is tangled with issues that *should* dissolve
under PEP 684 isolated-mode subinterpreters:

1. `fork_from_worker_thread()` / `run_subint_in_worker_thread()`
   internally allocate a **dedicated `threading.Thread`**
   rather than using `trio.to_thread.run_sync()`.
2. The test helper is named
   `run_fork_in_non_trio_thread()` — the
   `non_trio` qualifier is load-bearing today.

This doc catalogs *why* those constraints exist, which of
them isolated-mode would fix, and what the
audit-and-cleanup path looks like once msgspec #563 is
resolved.

The three reasons the constraints exist
---------------------------------------

### 1. GIL-starvation class → fixed by PEP 684 isolated mode

The class-A hang documented in
`subint_sigint_starvation_issue.md` is entirely about
legacy-config subints **sharing the main GIL**. Once
msgspec #563 lands and tractor flips
`tractor.spawn._subint` to
`concurrent.interpreters.create()` (isolated config), each
subint gets its own GIL. Abandoned subint threads can't
contend for main's GIL → can't starve the main trio loop
→ signal-wakeup-pipe drains normally → no SIGINT-drop.

This class of hazard **dissolves entirely**. The
non-trio-thread requirement for *this reason* disappears.

### 2. Destroy race / tstate-recycling → orthogonal; unclear

The `subint_proc` dedicated-thread fix (commit `26fb8206`)
addressed a different issue: `_interpreters.destroy(interp_id)`
was blocking on a trio-cache worker that had run an
earlier `interp.exec()` for that subint. Working
hypothesis at the time was "the cached thread retains the
subint's tstate".

But tstate-handling is **not specific to GIL mode** —
`_PyXI_Enter` / `_PyXI_Exit` (the C-level machinery both
configs use to enter/leave a subint from a thread) should
restore the caller's tstate regardless of GIL config. So
isolated mode **doesn't obviously fix this**. It might be:

- A py3.13 bug fixed in later versions — we saw the race
  first on 3.13 and never re-tested on 3.14 after moving
  to dedicated threads.
- A genuine CPython quirk around cached threads that
  exec'd into a subint, persisting across GIL modes.
- Something else we misdiagnosed — the empirical fix
  (dedicated thread) worked but the analysis may have
  been incomplete.

Only way to know: once we're on isolated mode, empirically
retry `trio.to_thread.run_sync(interp.exec, ...)` and see
if `destroy()` still blocks. If it does, keep the
dedicated thread; if not, one constraint relaxed.

### 3. Fork-from-main-interp-tstate (the constraint in this module's helper names)

The fork-from-main-interp-tstate invariant — CPython's
`PyOS_AfterFork_Child` →
`_PyInterpreterState_DeleteExceptMain` gate documented in
`subint_fork_blocked_by_cpython_post_fork_issue.md` — is
about the calling thread's **current** tstate at the
moment `os.fork()` runs. If trio's cache threads never
enter subints at all, their tstate is plain main-interp,
and fork from them would be fine.

The reason the smoke test +
`run_fork_in_non_trio_thread` test helper
currently use a dedicated `threading.Thread` is narrow:
**we don't want to risk a trio cache thread that has
previously been used as a subint driver being the one that
picks up the fork job**. If cached tstate doesn't get
cleared (back to reason #2), the fork's child-side
post-init would see the wrong interp and abort.

In an isolated-mode world where msgspec works:

- `subint_proc` would use the public
  `concurrent.interpreters.create()` + `Interpreter.exec()`
  / `Interpreter.close()` — which *should* handle tstate
  cleanly (they're the "blessed" API).
- If so, trio's cache threads are safe to fork from
  regardless of whether they've previously driven subints.
- → the `non_trio` qualifier in
  `run_fork_in_non_trio_thread` becomes
  *overcautious* rather than load-bearing, and the
  dedicated-thread primitives in `_subint_forkserver.py`
  can likely be replaced with straight
  `trio.to_thread.run_sync()` wrappers.

TL;DR
-----

| constraint | fixed by isolated mode? |
|---|---|
| GIL-starvation (class A) | **yes** |
| destroy race on cached worker | unclear — empirical test on py3.14 + isolated API required |
| fork-from-main-tstate requirement on worker | **probably yes, conditional on the destroy-race question above** |

If #2 also resolves on py3.14+ with isolated mode,
tractor could drop the `non_trio` qualifier from the fork
helper's name and just use `trio.to_thread.run_sync(...)`
for everything. But **we shouldn't do that preemptively**
— the current cautious design is cheap (one dedicated
thread per fork / per subint-exec) and correct.

Audit plan when msgspec #563 lands
----------------------------------

Assuming msgspec grows `Py_mod_multiple_interpreters`
support:

1. **Flip `tractor.spawn._subint` to isolated mode.** Drop
   the `_interpreters.create('legacy')` call in favor of
   the public API (`concurrent.interpreters.create()` +
   `Interpreter.exec()` / `Interpreter.close()`). Run the
   three `ai/conc-anal/subint_*_issue.md` reproducers —
   class-A (`test_stale_entry_is_deleted` etc.) should
   pass without the `skipon_spawn_backend('subint')` marks
   (revisit the marker inventory).

2. **Empirical destroy-race retest.** In `subint_proc`,
   swap the dedicated `threading.Thread` back to
   `trio.to_thread.run_sync(Interpreter.exec, ...,
   abandon_on_cancel=False)` and run the full subint test
   suite. If `Interpreter.close()` (or the backing
   destroy) blocks the same way as the legacy version
   did, revert and keep the dedicated thread.

3. **If #2 clean**, audit `_subint_forkserver.py`:
   - Rename `run_fork_in_non_trio_thread` → drop the
     `_non_trio_` qualifier (e.g. `run_fork_in_thread`) or
     inline the two-line `trio.to_thread.run_sync` call at
     the call sites and drop the helper entirely.
   - Consider whether `fork_from_worker_thread` +
     `run_subint_in_worker_thread` still warrant being
     separate module-level primitives or whether they
     collapse into a compound
     `trio.to_thread.run_sync`-driven pattern inside the
     (future) `subint_forkserver_proc` backend.

4. **Doc fallout.** `subint_sigint_starvation_issue.md`
   and `subint_cancel_delivery_hang_issue.md` both cite
   the legacy-GIL-sharing architecture as the root cause.
   Close them with commit-refs to the isolated-mode
   migration. This doc itself should get a closing
   post-mortem section noting which of #1/#2/#3 actually
   resolved vs persisted.

References
----------

- `tractor.spawn._subint_forkserver` — the in-tree module
  whose constraints this doc catalogs.
- `ai/conc-anal/subint_sigint_starvation_issue.md` — the
  GIL-starvation class.
- `ai/conc-anal/subint_cancel_delivery_hang_issue.md` —
  sibling Ctrl-C-able hang class.
- `ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md`
  — why fork-from-subint is blocked (this drives the
  forkserver-via-non-subint-thread workaround).
- `ai/conc-anal/subint_fork_from_main_thread_smoketest.py`
  — empirical validation for the workaround.
- [PEP 684 — per-interpreter GIL](https://peps.python.org/pep-0684/)
- [PEP 734 — `concurrent.interpreters` public API](https://peps.python.org/pep-0734/)
- [jcrist/msgspec#563 — PEP 684 support tracker](https://github.com/jcrist/msgspec/issues/563)
- tractor issue #379 — subint backend tracking.
