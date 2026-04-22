# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
Sub-interpreter (`subint`) actor spawning backend.

Spawns each sub-actor as a CPython PEP 734 sub-interpreter
(`concurrent.interpreters.Interpreter`) driven on its own OS
thread — same-process state isolation with faster start-up
than an OS subproc, while preserving tractor's existing
IPC-based actor boundary.

Availability
------------
Requires Python **3.14+**. The private `_interpreters` C
module we actually call into has shipped since 3.13, but
that vintage has a latent bug in its thread/subint
interaction which wedges tractor's spawn flow after
`_interpreters.create()` — the driver `threading.Thread`
silently never makes progress inside `_interpreters.exec()`.
(Minimal standalone reproductions with threading +
`_interpreters.exec()` work fine on 3.13; only our
multi-trio-task usage triggers the hang. 3.14 fixes it.)
On older runtimes the module still imports (so the registry
stays introspectable) but `subint_proc()` raises.

'''
from __future__ import annotations
import sys
import threading
from typing import (
    Any,
    TYPE_CHECKING,
)

import trio
from trio import TaskStatus


# NOTE: we reach into the *private* `_interpreters` C module
# for the actual subint create/exec/destroy calls rather than
# `concurrent.interpreters`' public API because the public API
# only exposes PEP 734's `'isolated'` config (per-interp GIL).
# Under `'isolated'`, any C extension missing the
# `Py_mod_multiple_interpreters` slot (PEP 684) refuses to
# import; in our stack that's `msgspec` — which tractor uses
# pervasively in the IPC layer — so isolated-mode subints
# can't finish booting the sub-actor's `trio.run()`. msgspec
# PEP 684 support is open upstream at jcrist/msgspec#563.
#
# Dropping to the `'legacy'` config keeps the main GIL + lets
# existing C extensions load normally while preserving the
# state isolation we actually care about for the actor model
# (separate `sys.modules` / `__main__` / globals).
#
# But — we feature-gate on the **public** `concurrent.interpreters`
# module (3.14+) even though we only call into the private
# `_interpreters` module. Reason: the private module has
# shipped since 3.13, but the thread/subint interactions
# tractor relies on (`threading.Thread` driving
# `_interpreters.exec(..., legacy)` while a trio loop runs in
# the parent + another inside the subint + IPC between them)
# hang silently on 3.13 and only work cleanly on 3.14. See
# docstring above for the empirical details. Using the public
# module's existence as the gate keeps this check honest.
#
# Migration path: when msgspec (jcrist/msgspec#563) and any
# other PEP 684-holdout C deps opt-in, we can switch to the
# public `concurrent.interpreters.create()` API (isolated
# mode) and pick up per-interp-GIL parallelism for free.
#
# References:
# - PEP 734 (`concurrent.interpreters` public API):
#   https://peps.python.org/pep-0734/
# - PEP 684 (per-interpreter GIL / `Py_mod_multiple_interpreters`):
#   https://peps.python.org/pep-0684/
# - stdlib docs (3.14+):
#   https://docs.python.org/3.14/library/concurrent.interpreters.html
# - CPython public wrapper source (`Lib/concurrent/interpreters/`):
#   https://github.com/python/cpython/tree/main/Lib/concurrent/interpreters
# - CPython private C ext source
#   (`Modules/_interpretersmodule.c`):
#   https://github.com/python/cpython/blob/main/Modules/_interpretersmodule.c
# - msgspec PEP 684 upstream tracker:
#   https://github.com/jcrist/msgspec/issues/563
try:
    # gate: presence of the public 3.14 stdlib wrapper (we
    # don't actually use it below, see NOTE above).
    from concurrent import interpreters as _public_interpreters  # noqa: F401  # type: ignore
    # actual driver: the private C module (also present on
    # 3.13 but we refuse that version — see gate above).
    import _interpreters  # type: ignore
    _has_subints: bool = True
except ImportError:
    _interpreters = None  # type: ignore
    _has_subints: bool = False


from tractor.log import get_logger
from tractor.msg import (
    types as msgtypes,
    pretty_struct,
)
from tractor.runtime._state import current_actor
from tractor.runtime._portal import Portal
from ._spawn import cancel_on_completion


if TYPE_CHECKING:
    from tractor.discovery._addr import UnwrappedAddress
    from tractor.ipc import (
        _server,
        Channel,
    )
    from tractor.runtime._runtime import Actor
    from tractor.runtime._supervise import ActorNursery


log = get_logger('tractor')


# How long we'll wait (in seconds) inside the shielded soft-kill
# / teardown blocks before abandoning the sub-interpreter to its
# fate. See the "hard-kill" comments at the two shield sites.
#
# Unbounded shields are a Bad Idea with subints: because CPython
# doesn't deliver SIGINT into sub-interpreters and the legacy
# config shares the main GIL, a stuck subint can otherwise lock
# the parent trio loop (and the user's Ctrl-C) indefinitely.
_HARD_KILL_TIMEOUT: float = 3.0


async def subint_proc(
    name: str,
    actor_nursery: ActorNursery,
    subactor: Actor,
    errors: dict[tuple[str, str], Exception],

    # passed through to actor main
    bind_addrs: list[UnwrappedAddress],
    parent_addr: UnwrappedAddress,
    _runtime_vars: dict[str, Any],  # serialized and sent to _child
    *,
    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED,
    proc_kwargs: dict[str, any] = {}

) -> None:
    '''
    Create a new sub-actor hosted inside a PEP 734
    sub-interpreter running on a dedicated OS thread,
    reusing tractor's existing UDS/TCP IPC handshake
    for parent<->child channel setup.

    Supervision model mirrors `trio_proc()`:
    - parent awaits `ipc_server.wait_for_peer()` for the
      child to connect back; on success yields a `Portal`
      via `task_status.started()`
    - on graceful shutdown we await the sub-interpreter's
      `trio.run()` completing naturally (driven by the
      child's actor runtime)
    - on cancellation we send `Portal.cancel_actor()` and
      then wait for the subint's trio loop to exit cleanly
      — unblocking the worker thread so the `Interpreter`
      can be closed

    '''
    if not _has_subints:
        raise RuntimeError(
            f'The {"subint"!r} spawn backend requires Python 3.14+.\n'
            f'(On py3.13 the private `_interpreters` C module '
            f'exists but tractor\'s spawn flow wedges — see '
            f'`tractor.spawn._subint` docstring for details.)\n'
            f'Current runtime: {sys.version}'
        )

    interp_id: int = _interpreters.create('legacy')
    log.runtime(
        f'Created sub-interpreter (legacy cfg) for sub-actor\n'
        f'(>\n'
        f' |_interp_id={interp_id}\n'
    )

    uid: tuple[str, str] = subactor.aid.uid
    loglevel: str | None = subactor.loglevel

    # Build a bootstrap code string driven via `_interpreters.exec()`.
    # All of `uid` (`tuple[str, str]`), `loglevel` (`str|None`),
    # `parent_addr` (`tuple[str, int|str]` — see `UnwrappedAddress`)
    # and `infect_asyncio` (`bool`) `repr()` to valid Python
    # literals, so we can embed them directly.
    bootstrap: str = (
        'from tractor._child import _actor_child_main\n'
        '_actor_child_main(\n'
        f'    uid={uid!r},\n'
        f'    loglevel={loglevel!r},\n'
        f'    parent_addr={parent_addr!r},\n'
        f'    infect_asyncio={infect_asyncio!r},\n'
        f'    spawn_method={"subint"!r},\n'
        ')\n'
    )

    cancelled_during_spawn: bool = False
    subint_exited = trio.Event()
    ipc_server: _server.Server = actor_nursery._actor.ipc_server

    # Capture a trio token so the driver thread can signal
    # `subint_exited.set()` back into the parent trio loop.
    trio_token = trio.lowlevel.current_trio_token()

    def _subint_target() -> None:
        '''
        Dedicated OS-thread target: runs `_interpreters.exec()`
        once and exits.

        We intentionally use a plain `threading.Thread` here
        rather than `trio.to_thread.run_sync()` because trio's
        thread cache would *recycle* the same OS thread for
        subsequent jobs — leaving CPython's subinterpreter
        tstate attached to that cached worker and blocking
        `_interpreters.destroy()` in the teardown block below.
        A dedicated thread truly exits after `exec()` returns,
        releasing the tstate so destroy can proceed.

        '''
        try:
            _interpreters.exec(interp_id, bootstrap)
        finally:
            try:
                trio.from_thread.run_sync(
                    subint_exited.set,
                    trio_token=trio_token,
                )
            except trio.RunFinishedError:
                # parent trio loop has already exited (proc
                # teardown); nothing to signal.
                pass

    # NOTE: `daemon=True` so a stuck subint can never block
    # process exit — if our `_HARD_KILL_TIMEOUT` paths below
    # have to abandon this thread, Python's interpreter
    # shutdown won't wait for it forever. Tradeoff: any
    # subint state still live at abandon-time may leak.
    driver_thread = threading.Thread(
        target=_subint_target,
        name=f'subint-driver[{interp_id}]',
        daemon=True,
    )

    try:
        try:
            driver_thread.start()

            try:
                event, chan = await ipc_server.wait_for_peer(uid)
            except trio.Cancelled:
                cancelled_during_spawn = True
                raise

            portal = Portal(chan)
            actor_nursery._children[uid] = (
                subactor,
                interp_id,  # proxy for the normal `proc` slot
                portal,
            )

            sspec = msgtypes.SpawnSpec(
                _parent_main_data=subactor._parent_main_data,
                enable_modules=subactor.enable_modules,
                reg_addrs=subactor.reg_addrs,
                bind_addrs=bind_addrs,
                _runtime_vars=_runtime_vars,
            )
            log.runtime(
                f'Sending spawn spec to subint child\n'
                f'{{}}=> {chan.aid.reprol()!r}\n'
                f'\n'
                f'{pretty_struct.pformat(sspec)}\n'
            )
            await chan.send(sspec)

            curr_actor: Actor = current_actor()
            curr_actor._actoruid2nursery[uid] = actor_nursery

            task_status.started(portal)

            with trio.CancelScope(shield=True):
                await actor_nursery._join_procs.wait()

            async with trio.open_nursery() as lifecycle_n:
                if portal in actor_nursery._cancel_after_result_on_exit:
                    lifecycle_n.start_soon(
                        cancel_on_completion,
                        portal,
                        subactor,
                        errors,
                    )

                # Soft-kill analog: wait for the subint to exit
                # naturally; on cancel, send a graceful cancel
                # via the IPC portal and then wait for the
                # driver thread to finish so `_interpreters.destroy()`
                # won't race with a running interpreter.
                try:
                    await subint_exited.wait()
                except trio.Cancelled:
                    # Bounded shield: we want to ATTEMPT a
                    # graceful cancel via the portal, but we
                    # MUST NOT let the shield trap user
                    # Ctrl-C / parent teardown forever if the
                    # subint is already unreachable (e.g., the
                    # IPC channel was broken — which is exactly
                    # what `test_ipc_channel_break_during_stream`
                    # exercises). After `_HARD_KILL_TIMEOUT` we
                    # drop the shield and let `Cancelled`
                    # propagate; the outer teardown will force
                    # things along.
                    with (
                        trio.CancelScope(shield=True),
                        trio.move_on_after(
                            _HARD_KILL_TIMEOUT,
                        ) as cs,
                    ):
                        log.cancel(
                            f'Soft-killing subint sub-actor\n'
                            f'c)=> {chan.aid.reprol()}\n'
                            f'   |_interp_id={interp_id}\n'
                        )
                        try:
                            await portal.cancel_actor()
                        except (
                            trio.BrokenResourceError,
                            trio.ClosedResourceError,
                        ):
                            # channel already down — subint will
                            # exit on its own timeline (or won't,
                            # in which case the timeout below
                            # is our escape).
                            pass
                        await subint_exited.wait()
                    if cs.cancelled_caught:
                        log.warning(
                            f'Soft-kill of subint sub-actor timed '
                            f'out after {_HARD_KILL_TIMEOUT}s — '
                            f'subint may still be running; '
                            f'escalating to thread-abandon.\n'
                            f'   |_interp_id={interp_id}\n'
                            f'   |_aid={chan.aid.reprol()}\n'
                        )
                    raise
                finally:
                    lifecycle_n.cancel_scope.cancel()

        finally:
            # Ensure the driver thread is *fully* joined before
            # destroying the subint. `subint_exited.set()` fires
            # from inside the thread but returns to trio before
            # the thread's bootstrap cleanup finishes; calling
            # `destroy()` too eagerly can race with tstate
            # teardown. Off-load the blocking `.join()` to a
            # cache thread (which carries no subint tstate of
            # its own, so no cache conflict).
            #
            # Bounded shield: if the driver thread never exits
            # (soft-kill failed above, subint stuck in
            # non-checkpointing Python, etc.) we MUST abandon
            # it rather than wedge the parent forever. The
            # thread is `daemon=True` so proc-exit won't block
            # on it either. Subsequent `_interpreters.destroy()`
            # on a still-running subint raises `InterpreterError`
            # which we log and swallow — the abandoned subint
            # will be torn down by process exit.
            with (
                trio.CancelScope(shield=True),
                trio.move_on_after(_HARD_KILL_TIMEOUT) as cs,
            ):
                if driver_thread.is_alive():
                    # XXX `abandon_on_cancel=True` is load-bearing:
                    # the default (False) makes `to_thread.run_sync`
                    # ignore the enclosing `move_on_after` and
                    # block until `driver_thread.join()` returns —
                    # which is exactly what we can't wait for here.
                    await trio.to_thread.run_sync(
                        driver_thread.join,
                        abandon_on_cancel=True,
                    )
            if cs.cancelled_caught:
                log.warning(
                    f'Subint driver thread did not exit within '
                    f'{_HARD_KILL_TIMEOUT}s — abandoning.\n'
                    f'   |_interp_id={interp_id}\n'
                    f'   |_thread={driver_thread.name}\n'
                    f'(This usually means portal-cancel could '
                    f'not be delivered — e.g., IPC channel was '
                    f'already broken. The subint will continue '
                    f'running until process exit terminates the '
                    f'daemon thread.)'
                )

            with trio.CancelScope(shield=True):
                try:
                    _interpreters.destroy(interp_id)
                    log.runtime(
                        f'Destroyed sub-interpreter\n'
                        f')>\n'
                        f' |_interp_id={interp_id}\n'
                    )
                except _interpreters.InterpreterError as e:
                    log.warning(
                        f'Could not destroy sub-interpreter '
                        f'{interp_id}: {e}\n'
                        f'(expected if the driver thread was '
                        f'abandoned above; the subint is still '
                        f'running and will be reaped at process '
                        f'exit.)'
                    )

    finally:
        if not cancelled_during_spawn:
            actor_nursery._children.pop(uid, None)


# ============================================================
# WIP PROTOTYPE — `subint_fork_proc`
# ============================================================
# Experimental: use a sub-interpreter purely as a launchpad
# from which to `os.fork()`, sidestepping the well-known
# trio+fork issues (python-trio/trio#1614 etc.) by guaranteeing
# the forking interp hasn't ever imported / run `trio`.
#
# The current `tractor.spawn._trio` backend already spawns a
# subprocess and has the child connect back to the parent
# over IPC. THIS prototype only changes *how* the subproc
# comes into existence — everything downstream (parent-side
# `ipc_server.wait_for_peer()`, `SpawnSpec`, `Portal` yield,
# soft-kill) is reused verbatim.
#
# Reference: issue #379's "Our own thoughts, ideas for
# fork()-workaround/hacks..." section.
# ============================================================


async def subint_fork_proc(
    name: str,
    actor_nursery: ActorNursery,
    subactor: Actor,
    errors: dict[tuple[str, str], Exception],

    # passed through to actor main
    bind_addrs: list[UnwrappedAddress],
    parent_addr: UnwrappedAddress,
    _runtime_vars: dict[str, Any],
    *,
    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED,
    proc_kwargs: dict[str, any] = {},

) -> None:
    '''
    EXPERIMENTAL / WIP: `trio`-safe `fork()` via a pristine
    sub-interpreter launchpad.

    Core trick
    ----------
    Create a fresh subint that has *never* imported `trio`.
    From a worker thread, drive that subint to call
    `os.fork()`. In the forked CHILD process, `exec()` back
    into `python -m tractor._child` (a fresh process). In the
    fork PARENT (still inside the launchpad subint), do
    nothing — just let the subint's `exec` call return and
    the worker thread exit. The parent-side trio task then
    waits for the child process to connect back using the
    same `ipc_server.wait_for_peer()` flow as `trio_proc`.

    Why this matters
    ----------------
    The existing `trio_proc` backend spawns a subprocess via
    `trio.lowlevel.open_process()` which ultimately uses
    `posix_spawn()` (or `fork+exec`) from the parent's main
    interpreter — the one running `trio.run()`. That path is
    affected by the trio+fork issues tracked in
    python-trio/trio#1614 and related, some of which are
    side-stepped only incidentally because we always `exec()`
    immediately after fork.

    By forking from a pristine subint instead, we have a
    known-clean-of-trio fork parent. If we later want to try
    **fork-without-exec** for faster startup and automatic
    parent-`__main__` inheritance (the property `mp.fork`
    gives for free), this approach could unlock that cleanly.

    Relationship to the other backends
    ----------------------------------
    - `trio_proc`: fork/exec from main interp → affected by
      trio+fork issues, solved via immediate exec.
    - `subint_proc`: in-process subint, no fork at all →
      affected by shared-GIL abandoned-thread hazards (see
      `ai/conc-anal/subint_sigint_starvation_issue.md`).
    - `subint_fork_proc` (THIS): OS-level subproc (like
      `trio_proc`) BUT forked from a trio-free subint →
      avoids both issue-classes above, at the cost of an
      extra subint create/destroy per spawn.

    Status
    ------
    **NOT IMPLEMENTED** beyond the bootstrap scaffolding
    below. Open questions needing empirical validation:

    1. Does CPython allow `os.fork()` from a non-main
       sub-interpreter under the legacy config? The public
       API is silent; there may be PEP 684 safety guards.
    2. Does the forked child need to fully `exec()` or can
       we stay fork-without-exec and `trio.run()` directly
       from within the launchpad subint in the child? The
       latter is the "interesting" mode — faster startup,
       `__main__` inheritance — but opens the question of
       what residual state from the parent's main interp
       leaks into the child's subint.
    3. How do `signal.set_wakeup_fd()`, installed signal
       handlers, and other process-global state interact
       when the forking thread is inside a subint? The
       child presumably inherits them but a fresh
       `trio.run()` resets what it cares about.

    '''
    if not _has_subints:
        raise RuntimeError(
            f'The {"subint_fork"!r} spawn backend requires '
            f'Python 3.14+ (private stdlib `_interpreters` C '
            f'module + tractor-usage stability).\n'
            f'Current runtime: {sys.version}'
        )

    raise NotImplementedError(
        '`subint_fork_proc` is a WIP prototype scaffold — '
        'the driver thread + fork-bootstrap + connect-back '
        'orchestration below is not yet wired up. See '
        'issue #379 for context.\n'
        '(Structure kept in-tree so the next iteration has '
        'a concrete starting point rather than a blank page.)'
    )

    # ------------------------------------------------------------
    # SKETCH (below is intentionally dead code; kept so reviewers
    # can see the shape we'd plausibly build up to). Roughly
    # mirrors `subint_proc` structure but WITHOUT the in-process
    # subint lifetime management — the subint only lives long
    # enough to call `os.fork()`.
    # ------------------------------------------------------------

    # Create the launchpad subint. Legacy config matches
    # `subint_proc`'s reasoning (msgspec / PEP 684). For
    # fork-via-subint, isolation is moot since we don't
    # *stay* in the subint — we just need it trio-free.
    interp_id: int = _interpreters.create('legacy')
    log.runtime(
        f'Created launchpad subint for fork-spawn\n'
        f'(>\n'
        f' |_interp_id={interp_id}\n'
    )

    uid: tuple[str, str] = subactor.aid.uid
    loglevel: str | None = subactor.loglevel

    # Bootstrap fires inside the launchpad subint on a
    # worker OS-thread. Calls `os.fork()`. In the child,
    # `execv` back into the existing `python -m tractor._child`
    # CLI entry — which is what `trio_proc` already uses — so
    # the connect-back dance is identical. In the fork-parent
    # (still in the launchpad subint), return so the thread
    # can exit and we can `_interpreters.destroy()` the
    # launchpad.
    #
    # NOTE, `os.execv()` replaces the entire process image
    # (all interps, all threads — CPython handles this at the
    # OS level), so subint cleanup in the child is a no-op.
    import shlex
    uid_repr: str = repr(str(uid))
    parent_addr_repr: str = repr(str(parent_addr))
    bootstrap: str = (
        'import os, sys\n'
        'pid = os.fork()\n'
        'if pid == 0:\n'
        '    # CHILD: full `exec` into fresh Python for\n'
        '    # maximum isolation. (A `fork`-without-exec\n'
        '    # variant would skip this and call\n'
        '    # `_actor_child_main` directly — see class\n'
        '    # docstring "Open question 2".)\n'
        '    os.execv(\n'
        '        sys.executable,\n'
        '        [\n'
        '            sys.executable,\n'
        "            '-m',\n"
        "            'tractor._child',\n"
        f'            {shlex.quote("--uid")!r},\n'
        f'            {uid_repr},\n'
        f'            {shlex.quote("--parent_addr")!r},\n'
        f'            {parent_addr_repr},\n'
        + (
            f'            {shlex.quote("--loglevel")!r},\n'
            f'            {loglevel!r},\n'
            if loglevel else ''
        )
        + (
            f'            {shlex.quote("--asyncio")!r},\n'
            if infect_asyncio else ''
        )
        + '        ],\n'
        '    )\n'
        '# FORK-PARENT branch falls through — we just want\n'
        '# the launchpad subint to finish so the driver\n'
        '# thread exits.\n'
    )

    # TODO: orchestrate driver thread (mirror `subint_proc`'s
    # `_subint_target` pattern), then await
    # `ipc_server.wait_for_peer(uid)` on the parent side —
    # same as `trio_proc`. Soft-kill path is simpler here
    # than in `subint_proc`: we're managing an OS subproc,
    # not a legacy subint, so `Portal.cancel_actor()` + wait
    # + OS-level `SIGKILL` fallback (like `trio_proc`'s
    # `hard_kill()`) applies directly.
