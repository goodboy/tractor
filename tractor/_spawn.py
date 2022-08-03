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

"""
Machinery for actor process spawning using multiple backends.

"""
from __future__ import annotations
import sys
import platform
from typing import (
    Any, Optional, Callable, TypeVar, TYPE_CHECKING
)
from collections.abc import Awaitable

import trio
from trio_typing import TaskStatus

from ._debug import (
    maybe_wait_for_debugger,
    acquire_debug_lock,
)
from ._state import (
    current_actor,
    is_main_process,
    is_root_process,
    debug_mode,
)

from .log import get_logger
from ._portal import Portal
from ._actor import Actor
from ._entry import _mp_main
from ._exceptions import ActorFailure


if TYPE_CHECKING:
    import multiprocessing as mp
    ProcessType = TypeVar('ProcessType', mp.Process, trio.Process)

log = get_logger('tractor')

# placeholder for an mp start context if so using that backend
_ctx: Optional[mp.context.BaseContext] = None
_spawn_method: str = "trio"


if platform.system() == 'Windows':

    import multiprocessing as mp
    _ctx = mp.get_context("spawn")

    async def proc_waiter(proc: mp.Process) -> None:
        await trio.lowlevel.WaitForSingleObject(proc.sentinel)
else:
    # *NIX systems use ``trio`` primitives as our default as well

    async def proc_waiter(proc: mp.Process) -> None:
        await trio.lowlevel.wait_readable(proc.sentinel)


def try_set_start_method(name: str) -> Optional[mp.context.BaseContext]:
    '''
    Attempt to set the method for process starting, aka the "actor
    spawning backend".

    If the desired method is not supported this function will error.
    On Windows only the ``multiprocessing`` "spawn" method is offered
    besides the default ``trio`` which uses async wrapping around
    ``subprocess.Popen``.

    '''
    import multiprocessing as mp
    global _ctx
    global _spawn_method

    methods = mp.get_all_start_methods()
    if 'fork' in methods:
        # forking is incompatible with ``trio``s global task tree
        methods.remove('fork')

    # supported on all platforms
    methods += ['trio']

    if name not in methods:
        raise ValueError(
            f"Spawn method `{name}` is invalid please choose one of {methods}"
        )
    elif name == 'forkserver':
        from . import _forkserver_override
        _forkserver_override.override_stdlib()
        _ctx = mp.get_context(name)
    elif name == 'trio':
        _ctx = None
    else:
        _ctx = mp.get_context(name)

    _spawn_method = name
    return _ctx


async def exhaust_portal(

    portal: Portal,
    actor: Actor

) -> Any:
    '''
    Pull final result from portal (assuming it has one).

    If the main task is an async generator do our best to consume
    what's left of it.
    '''
    try:
        log.debug(f"Waiting on final result from {actor.uid}")

        # XXX: streams should never be reaped here since they should
        # always be established and shutdown using a context manager api
        final = await portal.result()

    except (Exception, trio.MultiError) as err:
        # we reraise in the parent task via a ``trio.MultiError``
        return err
    except trio.Cancelled as err:
        # lol, of course we need this too ;P
        # TODO: merge with above?
        log.warning(f"Cancelled result waiter for {portal.actor.uid}")
        return err
    else:
        log.debug(f"Returning final result: {final}")
        return final


async def cancel_on_completion(

    portal: Portal,
    actor: Actor,
    errors: dict[tuple[str, str], Exception],

) -> None:
    '''
    Cancel actor gracefully once it's "main" portal's
    result arrives.

    Should only be called for actors spawned with `run_in_actor()`.

    '''
    # if this call errors we store the exception for later
    # in ``errors`` which will be reraised inside
    # a MultiError and we still send out a cancel request
    result = await exhaust_portal(portal, actor)
    if isinstance(result, Exception):
        errors[actor.uid] = result
        log.warning(
            f"Cancelling {portal.channel.uid} after error {result}"
        )

    else:
        log.runtime(
            f"Cancelling {portal.channel.uid} gracefully "
            f"after result {result}")

    # cancel the process now that we have a final result
    await portal.cancel_actor()


async def do_hard_kill(
    proc: trio.Process,
    terminate_after: int = 3,
) -> None:
    # NOTE: this timeout used to do nothing since we were shielding
    # the ``.wait()`` inside ``new_proc()`` which will pretty much
    # never release until the process exits, now it acts as
    # a hard-kill time ultimatum.
    with trio.move_on_after(terminate_after) as cs:

        # NOTE: This ``__aexit__()`` shields internally.
        async with proc:  # calls ``trio.Process.aclose()``
            log.debug(f"Terminating {proc}")

    if cs.cancelled_caught:
        # XXX: should pretty much never get here unless we have
        # to move the bits from ``proc.__aexit__()`` out and
        # into here.
        log.critical(f"#ZOMBIE_LORD_IS_HERE: {proc}")
        proc.kill()


async def soft_wait(

    proc: ProcessType,
    wait_func: Callable[
        [ProcessType],
        Awaitable,
    ],
    portal: Portal,

) -> None:
    # Wait for proc termination but **dont' yet** call
    # ``trio.Process.__aexit__()`` (it tears down stdio
    # which will kill any waiting remote pdb trace).
    # This is a "soft" (cancellable) join/reap.
    uid = portal.channel.uid
    try:
        log.cancel(f'Soft waiting on actor:\n{uid}')
        await wait_func(proc)
    except trio.Cancelled:
        # if cancelled during a soft wait, cancel the child
        # actor before entering the hard reap sequence
        # below. This means we try to do a graceful teardown
        # via sending a cancel message before getting out
        # zombie killing tools.
        async with trio.open_nursery() as n:
            n.cancel_scope.shield = True

            async def cancel_on_proc_deth():
                '''
                Cancel the actor cancel request if we detect that
                that the process terminated.

                '''
                await wait_func(proc)
                n.cancel_scope.cancel()

            n.start_soon(cancel_on_proc_deth)
            await portal.cancel_actor()

            if proc.poll() is None:  # type: ignore
                log.warning(
                    f'Process still alive after cancel request:\n{uid}')

                n.cancel_scope.cancel()
        raise


async def new_proc(

    name: str,
    actor_nursery: 'ActorNursery',  # type: ignore  # noqa
    subactor: Actor,
    errors: dict[tuple[str, str], Exception],

    # passed through to actor main
    bind_addr: tuple[str, int],
    parent_addr: tuple[str, int],
    _runtime_vars: dict[str, Any],  # serialized and sent to _child

    *,

    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED

) -> None:
    '''
    Create a new ``Process`` using a "spawn method" as (configured using
    ``try_set_start_method()``).

    This routine should be started in a actor runtime task and the logic
    here is to be considered the core supervision strategy.

    '''
    # mark the new actor with the global spawn method
    subactor._spawn_method = _spawn_method
    uid = subactor.uid

    if _spawn_method == 'trio':
        spawn_cmd = [
            sys.executable,
            "-m",
            # Hardcode this (instead of using ``_child.__name__`` to avoid a
            # double import warning: https://stackoverflow.com/a/45070583
            "tractor._child",
            # We provide the child's unique identifier on this exec/spawn
            # line for debugging purposes when viewing the process tree from
            # the OS; it otherwise can be passed via the parent channel if
            # we prefer in the future (for privacy).
            "--uid",
            str(subactor.uid),
            # Address the child must connect to on startup
            "--parent_addr",
            str(parent_addr)
        ]

        if subactor.loglevel:
            spawn_cmd += [
                "--loglevel",
                subactor.loglevel
            ]
        # Tell child to run in guest mode on top of ``asyncio`` loop
        if infect_asyncio:
            spawn_cmd.append("--asyncio")

        cancelled_during_spawn: bool = False
        proc: Optional[trio.Process] = None
        try:
            try:
                # TODO: needs ``trio_typing`` patch?
                proc = await trio.lowlevel.open_process(spawn_cmd)  # type: ignore

                log.runtime(f"Started {proc}")

                # wait for actor to spawn and connect back to us
                # channel should have handshake completed by the
                # local actor by the time we get a ref to it
                event, chan = await actor_nursery._actor.wait_for_peer(
                    subactor.uid)

            except trio.Cancelled:
                cancelled_during_spawn = True
                # we may cancel before the child connects back in which
                # case avoid clobbering the pdb tty.
                if debug_mode():
                    with trio.CancelScope(shield=True):
                        # don't clobber an ongoing pdb
                        if is_root_process():
                            await maybe_wait_for_debugger()

                        elif proc is not None:
                            async with acquire_debug_lock(uid):
                                # soft wait on the proc to terminate
                                with trio.move_on_after(0.5):
                                    await proc.wait()
                raise

            # a sub-proc ref **must** exist now
            assert proc

            portal = Portal(chan)
            actor_nursery._children[subactor.uid] = (
                subactor, proc, portal)

            # send additional init params
            await chan.send({
                "_parent_main_data": subactor._parent_main_data,
                "enable_modules": subactor.enable_modules,
                "_arb_addr": subactor._arb_addr,
                "bind_host": bind_addr[0],
                "bind_port": bind_addr[1],
                "_runtime_vars": _runtime_vars,
            })

            # track subactor in current nursery
            curr_actor = current_actor()
            curr_actor._actoruid2nursery[subactor.uid] = actor_nursery

            # resume caller at next checkpoint now that child is up
            task_status.started(portal)

            # wait for ActorNursery.wait() to be called
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
                await soft_wait(
                    proc,
                    trio.Process.wait,
                    portal
                )

                # cancel result waiter that may have been spawned in
                # tandem if not done already
                log.warning(
                    "Cancelling existing result waiter task for "
                    f"{subactor.uid}")
                nursery.cancel_scope.cancel()

        finally:
            # The "hard" reap since no actor zombies are allowed!
            # XXX: do this **after** cancellation/tearfown to avoid
            # killing the process too early.
            if proc:
                log.cancel(f'Hard reap sequence starting for {uid}')
                with trio.CancelScope(shield=True):

                    # don't clobber an ongoing pdb
                    if cancelled_during_spawn:
                        # Try again to avoid TTY clobbering.
                        async with acquire_debug_lock(uid):
                            with trio.move_on_after(0.5):
                                await proc.wait()

                    if is_root_process():
                        await maybe_wait_for_debugger(
                            child_in_debug=_runtime_vars.get(
                                '_debug_mode', False),
                        )

                    if proc.poll() is None:
                        log.cancel(f"Attempting to hard kill {proc}")
                        await do_hard_kill(proc)

                    log.debug(f"Joined {proc}")
            else:
                log.warning('Nursery cancelled before sub-proc started')

            if not cancelled_during_spawn:
                # pop child entry to indicate we no longer managing this
                # subactor
                actor_nursery._children.pop(subactor.uid)

    else:
        # `multiprocessing`
        # async with trio.open_nursery() as nursery:
        await mp_new_proc(
            name=name,
            actor_nursery=actor_nursery,
            subactor=subactor,
            errors=errors,

            # passed through to actor main
            bind_addr=bind_addr,
            parent_addr=parent_addr,
            _runtime_vars=_runtime_vars,
            infect_asyncio=infect_asyncio,
            task_status=task_status,
        )


async def mp_new_proc(

    name: str,
    actor_nursery: 'ActorNursery',  # type: ignore  # noqa
    subactor: Actor,
    errors: dict[tuple[str, str], Exception],
    # passed through to actor main
    bind_addr: tuple[str, int],
    parent_addr: tuple[str, int],
    _runtime_vars: dict[str, Any],  # serialized and sent to _child
    *,
    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED

) -> None:

    # uggh zone
    try:
        from multiprocessing import semaphore_tracker  # type: ignore
        resource_tracker = semaphore_tracker
        resource_tracker._resource_tracker = resource_tracker._semaphore_tracker  # noqa
    except ImportError:
        # 3.8 introduces a more general version that also tracks shared mems
        from multiprocessing import resource_tracker  # type: ignore

    assert _ctx
    start_method = _ctx.get_start_method()
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
                fs._forkserver_address,
                fs._forkserver_alive_fd,
                getattr(fs, '_forkserver_pid', None),
                getattr(
                    resource_tracker._resource_tracker, '_pid', None),
                resource_tracker._resource_tracker._fd,
            )
        else:
            assert curr_actor._forkserver_info
            fs_info = (
                fs._forkserver_address,
                fs._forkserver_alive_fd,
                fs._forkserver_pid,
                resource_tracker._resource_tracker._pid,
                resource_tracker._resource_tracker._fd,
             ) = curr_actor._forkserver_info
    else:
        fs_info = (None, None, None, None, None)

    proc: mp.Process = _ctx.Process(  # type: ignore
        target=_mp_main,
        args=(
            subactor,
            bind_addr,
            fs_info,
            start_method,
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
    actor_nursery._children[subactor.uid] = (subactor, proc, None)

    proc.start()
    if not proc.is_alive():
        raise ActorFailure("Couldn't start sub-actor?")

    log.runtime(f"Started {proc}")

    try:
        # wait for actor to spawn and connect back to us
        # channel should have handshake completed by the
        # local actor by the time we get a ref to it
        event, chan = await actor_nursery._actor.wait_for_peer(
            subactor.uid)

        # XXX: monkey patch poll API to match the ``subprocess`` API..
        # not sure why they don't expose this but kk.
        proc.poll = lambda: proc.exitcode  # type: ignore

    # except:
        # TODO: in the case we were cancelled before the sub-proc
        # registered itself back we must be sure to try and clean
        # any process we may have started.

        portal = Portal(chan)
        actor_nursery._children[subactor.uid] = (subactor, proc, portal)

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
            await soft_wait(
                proc,
                proc_waiter,
                portal
            )

            # cancel result waiter that may have been spawned in
            # tandem if not done already
            log.warning(
                "Cancelling existing result waiter task for "
                f"{subactor.uid}")
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
        subactor, proc, portal = actor_nursery._children.pop(subactor.uid)
