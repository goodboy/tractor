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
Runs on py3.13+ via the *private* stdlib `_interpreters` C
module (which predates the py3.14 public
`concurrent.interpreters` stdlib wrapper). See the comment
above the `_interpreters` import below for the trade-offs
driving the private-API choice. On older runtimes the
module still imports (so the registry stays
introspectable) but `subint_proc()` raises.

'''
from __future__ import annotations
import sys
from functools import partial
from typing import (
    Any,
    TYPE_CHECKING,
)

import trio
from trio import TaskStatus


# NOTE: we reach into the *private* `_interpreters` C module
# rather than `concurrent.interpreters`' public API because the
# public API only exposes PEP 734's `'isolated'` config
# (per-interp GIL). Under `'isolated'`, any C extension missing
# the `Py_mod_multiple_interpreters` slot (PEP 684) refuses to
# import; in our stack that's `msgspec` — which tractor uses
# pervasively in the IPC layer — so isolated-mode subints can't
# finish booting the sub-actor's `trio.run()`. msgspec PEP 684
# support is open upstream at jcrist/msgspec#563.
#
# Dropping to the `'legacy'` config keeps the main GIL + lets
# existing C extensions load normally while preserving the
# state isolation we actually care about for the actor model
# (separate `sys.modules` / `__main__` / globals). Side win:
# the private `_interpreters` module has shipped since py3.13
# (it predates the PEP 734 stdlib landing), so the `subint`
# backend can run on py3.13+ despite `concurrent.interpreters`
# itself being 3.14+.
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
            f'The {"subint"!r} spawn backend requires Python 3.13+ '
            f'(private stdlib `_interpreters` C module; the public '
            f'`concurrent.interpreters` wrapper lands in py3.14).\n'
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

    async def _drive_subint() -> None:
        '''
        Block a worker OS-thread on `_interpreters.exec()` for
        the lifetime of the sub-actor. When the subint's inner
        `trio.run()` exits, `exec()` returns and the thread
        naturally joins.

        '''
        try:
            await trio.to_thread.run_sync(
                _interpreters.exec,
                interp_id,
                bootstrap,
                abandon_on_cancel=False,
            )
        finally:
            subint_exited.set()

    try:
        try:
            async with trio.open_nursery() as thread_n:
                thread_n.start_soon(_drive_subint)

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
                    # driver thread to finish so `interp.close()`
                    # won't race with a running interpreter.
                    try:
                        await subint_exited.wait()
                    except trio.Cancelled:
                        with trio.CancelScope(shield=True):
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
                                # exit on its own timeline
                                pass
                            await subint_exited.wait()
                        raise
                    finally:
                        lifecycle_n.cancel_scope.cancel()

        finally:
            # The driver thread has exited (either natural subint
            # completion or post-cancel teardown) so the subint is
            # no longer running — safe to destroy.
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
                        f'{interp_id}: {e}'
                    )

    finally:
        if not cancelled_during_spawn:
            actor_nursery._children.pop(uid, None)
