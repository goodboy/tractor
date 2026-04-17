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
The `multiprocessing` subprocess spawning backends (`spawn`
and `forkserver` variants).

Driven by the stdlib `multiprocessing` context selected via
`try_set_start_method()` in the `_spawn` core module, which
sets the module-global `_ctx` and `_spawn_method` read here.

'''
from __future__ import annotations
import multiprocessing as mp
from typing import (
    Any,
    TYPE_CHECKING,
)

import trio
from trio import TaskStatus

from tractor.runtime._state import (
    current_actor,
    is_main_process,
)
from tractor.log import get_logger
from tractor.discovery._addr import UnwrappedAddress
from tractor.runtime._portal import Portal
from tractor.runtime._runtime import Actor
from tractor._exceptions import ActorFailure
from ._entry import _mp_main
# NOTE: module-import (not from-import) so we dynamically see
# the *current* `_ctx` / `_spawn_method` values, which are mutated
# by `try_set_start_method()` after module load time.
from . import _spawn
from ._spawn import (
    cancel_on_completion,
    proc_waiter,
    soft_kill,
)


if TYPE_CHECKING:
    from tractor.ipc import (
        _server,
    )
    from tractor.runtime._supervise import ActorNursery


log = get_logger('tractor')


async def mp_proc(
    name: str,
    actor_nursery: ActorNursery,  # type: ignore  # noqa
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

    # uggh zone
    try:
        from multiprocessing import semaphore_tracker  # type: ignore
        resource_tracker = semaphore_tracker
        resource_tracker._resource_tracker = resource_tracker._semaphore_tracker  # noqa
    except ImportError:
        # 3.8 introduces a more general version that also tracks shared mems
        from multiprocessing import resource_tracker  # type: ignore

    assert _spawn._ctx
    start_method = _spawn._ctx.get_start_method()
    if start_method == 'forkserver':

        from multiprocessing import forkserver  # type: ignore
        # XXX do our hackery on the stdlib to avoid multiple
        # forkservers (one at each subproc layer).
        fs = forkserver._forkserver
        curr_actor = current_actor()
        if is_main_process() and not curr_actor._forkserver_info:
            # if we're the "main" process start the forkserver
            # only once and pass its ipc info to downstream
            # children
            # forkserver.set_forkserver_preload(enable_modules)
            forkserver.ensure_running()
            fs_info = (
                fs._forkserver_address,  # type: ignore  # noqa
                fs._forkserver_alive_fd,  # type: ignore  # noqa
                getattr(fs, '_forkserver_pid', None),
                getattr(
                    resource_tracker._resource_tracker, '_pid', None),
                resource_tracker._resource_tracker._fd,
            )
        else:  # request to forkerserver to fork a new child
            assert curr_actor._forkserver_info
            fs_info = (
                fs._forkserver_address,  # type: ignore  # noqa
                fs._forkserver_alive_fd,  # type: ignore  # noqa
                fs._forkserver_pid,  # type: ignore  # noqa
                resource_tracker._resource_tracker._pid,
                resource_tracker._resource_tracker._fd,
             ) = curr_actor._forkserver_info
    else:
        # spawn method
        fs_info = (None, None, None, None, None)

    proc: mp.Process = _spawn._ctx.Process(  # type: ignore
        target=_mp_main,
        args=(
            subactor,
            bind_addrs,
            fs_info,
            _spawn._spawn_method,
            parent_addr,
            infect_asyncio,
        ),
        # daemon=True,
        name=name,
    )

    # `multiprocessing` only (since no async interface):
    # register the process before start in case we get a cancel
    # request before the actor has fully spawned - then we can wait
    # for it to fully come up before sending a cancel request
    actor_nursery._children[subactor.aid.uid] = (subactor, proc, None)

    proc.start()
    if not proc.is_alive():
        raise ActorFailure("Couldn't start sub-actor?")

    log.runtime(f"Started {proc}")

    ipc_server: _server.Server = actor_nursery._actor.ipc_server
    try:
        # wait for actor to spawn and connect back to us
        # channel should have handshake completed by the
        # local actor by the time we get a ref to it
        event, chan = await ipc_server.wait_for_peer(
            subactor.aid.uid,
        )

        # XXX: monkey patch poll API to match the ``subprocess`` API..
        # not sure why they don't expose this but kk.
        proc.poll = lambda: proc.exitcode  # type: ignore

    # except:
        # TODO: in the case we were cancelled before the sub-proc
        # registered itself back we must be sure to try and clean
        # any process we may have started.

        portal = Portal(chan)
        actor_nursery._children[subactor.aid.uid] = (subactor, proc, portal)

        # unblock parent task
        task_status.started(portal)

        # wait for ``ActorNursery`` block to signal that
        # subprocesses can be waited upon.
        # This is required to ensure synchronization
        # with user code that may want to manually await results
        # from nursery spawned sub-actors. We don't want the
        # containing nurseries here to collect results or error
        # while user code is still doing it's thing. Only after the
        # nursery block closes do we allow subactor results to be
        # awaited and reported upwards to the supervisor.
        with trio.CancelScope(shield=True):
            await actor_nursery._join_procs.wait()

        async with trio.open_nursery() as nursery:
            if portal in actor_nursery._cancel_after_result_on_exit:
                nursery.start_soon(
                    cancel_on_completion,
                    portal,
                    subactor,
                    errors
                )

            # This is a "soft" (cancellable) join/reap which
            # will remote cancel the actor on a ``trio.Cancelled``
            # condition.
            await soft_kill(
                proc,
                proc_waiter,
                portal
            )

            # cancel result waiter that may have been spawned in
            # tandem if not done already
            log.warning(
                "Cancelling existing result waiter task for "
                f"{subactor.aid.uid}")
            nursery.cancel_scope.cancel()

    finally:
        # hard reap sequence
        if proc.is_alive():
            log.cancel(f"Attempting to hard kill {proc}")
            with trio.move_on_after(0.1) as cs:
                cs.shield = True
                await proc_waiter(proc)

            if cs.cancelled_caught:
                proc.terminate()

        proc.join()
        log.debug(f"Joined {proc}")

        # pop child entry to indicate we are no longer managing subactor
        actor_nursery._children.pop(subactor.aid.uid)

        # TODO: prolly report to ``mypy`` how this causes all sorts of
        # false errors..
        # subactor, proc, portal = actor_nursery._children.pop(subactor.uid)
