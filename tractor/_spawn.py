"""
Process spawning.

Mostly just wrapping around ``multiprocessing``.
"""
import inspect
import multiprocessing as mp
import platform
from typing import Any, List, Dict

import trio
import trio_run_in_process
from trio_typing import TaskStatus
from async_generator import aclosing

try:
    from multiprocessing import semaphore_tracker  # type: ignore
    resource_tracker = semaphore_tracker
    resource_tracker._resource_tracker = resource_tracker._semaphore_tracker
except ImportError:
    # 3.8 introduces a more general version that also tracks shared mems
    from multiprocessing import resource_tracker  # type: ignore

from multiprocessing import forkserver  # type: ignore
from typing import Tuple

from . import _forkserver_override
from ._state import current_actor
from .log import get_logger
from ._portal import Portal
from ._actor import Actor, ActorFailure


log = get_logger('tractor')

_ctx: mp.context.BaseContext = mp.get_context("spawn")  # type: ignore


if platform.system() == 'Windows':
    async def proc_waiter(proc: mp.Process) -> None:
        await trio.hazmat.WaitForSingleObject(proc.sentinel)
else:
    async def proc_waiter(proc: mp.Process) -> None:
        await trio.hazmat.wait_readable(proc.sentinel)


def try_set_start_method(name: str) -> mp.context.BaseContext:
    """Attempt to set the start method for ``multiprocess.Process`` spawning.

    If the desired method is not supported the sub-interpreter (aka "spawn"
    method) is used.
    """
    global _ctx

    allowed = mp.get_all_start_methods()

    if name not in allowed:
        name = 'spawn'
    elif name == 'fork':
        raise ValueError(
            "`fork` is unsupported due to incompatibility with `trio`"
        )
    elif name == 'forkserver':
        _forkserver_override.override_stdlib()

    assert name in allowed

    _ctx = mp.get_context(name)
    return _ctx


def is_main_process() -> bool:
    """Bool determining if this actor is running in the top-most process.
    """
    return mp.current_process().name == 'MainProcess'


async def exhaust_portal(
    portal: Portal,
    actor: Actor
) -> Any:
    """Pull final result from portal (assuming it has one).

    If the main task is an async generator do our best to consume
    what's left of it.
    """
    try:
        log.debug(f"Waiting on final result from {actor.uid}")
        final = res = await portal.result()
        # if it's an async-gen then alert that we're cancelling it
        if inspect.isasyncgen(res):
            final = []
            log.warning(
                f"Blindly consuming asyncgen for {actor.uid}")
            with trio.fail_after(1):
                async with aclosing(res) as agen:
                    async for item in agen:
                        log.debug(f"Consuming item {item}")
                        final.append(item)
    except (Exception, trio.MultiError) as err:
        # we reraise in the parent task via a ``trio.MultiError``
        return err
    else:
        return final


async def cancel_on_completion(
    portal: Portal,
    actor: Actor,
    errors: List[Exception],
    task_status=trio.TASK_STATUS_IGNORED,
) -> None:
    """Cancel actor gracefully once it's "main" portal's
    result arrives.

    Should only be called for actors spawned with `run_in_actor()`.
    """
    with trio.CancelScope() as cs:
        task_status.started(cs)
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
            log.info(f"Cancelling {portal.channel.uid} gracefully")

        # cancel the process now that we have a final result
        await portal.cancel_actor()

    # XXX: lol, this will never get run without a shield above..
    # if cs.cancelled_caught:
    #     log.warning(
    #         "Result waiter was cancelled, process may have died")


async def new_proc(
    name: str,
    actor_nursery: 'ActorNursery',
    subactor: Actor,
    errors: Dict[str, Exception],
    # passed through to actor main
    bind_addr: Tuple[str, int],
    parent_addr: Tuple[str, int],
    begin_wait_phase: trio.Event,
    use_trip: bool = True,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED
) -> mp.Process:
    """Create a new ``multiprocessing.Process`` using the
    spawn method as configured using ``try_set_start_method()``.
    """
    cancel_scope = None

    async with trio.open_nursery() as nursery:
        if use_trip:
            # trio_run_in_process
            async with trio_run_in_process.open_in_process(
                subactor._trip_main,
                bind_addr,
                parent_addr,
            ) as proc:
                log.info(f"Started {proc}")

                # wait for actor to spawn and connect back to us
                # channel should have handshake completed by the
                # local actor by the time we get a ref to it
                event, chan = await actor_nursery._actor.wait_for_peer(
                    subactor.uid)
                portal = Portal(chan)
                actor_nursery._children[subactor.uid] = (
                    subactor, proc, portal)
                task_status.started(portal)

                # wait for ActorNursery.wait() to be called
                await actor_nursery._join_procs.wait()

                if portal in actor_nursery._cancel_after_result_on_exit:
                    cancel_scope = await nursery.start(
                        cancel_on_completion, portal, subactor, errors)

                # TRIP blocks here until process is complete
        else:
            # `multiprocessing`
            start_method = _ctx.get_start_method()
            if start_method == 'forkserver':
                # XXX do our hackery on the stdlib to avoid multiple
                # forkservers (one at each subproc layer).
                fs = forkserver._forkserver
                curr_actor = current_actor()
                if is_main_process() and not curr_actor._forkserver_info:
                    # if we're the "main" process start the forkserver
                    # only once and pass its ipc info to downstream
                    # children
                    # forkserver.set_forkserver_preload(rpc_module_paths)
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

            proc = _ctx.Process(
                target=subactor._mp_main,
                args=(
                    bind_addr,
                    fs_info,
                    start_method,
                    parent_addr
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

            log.info(f"Started {proc}")

            # wait for actor to spawn and connect back to us
            # channel should have handshake completed by the
            # local actor by the time we get a ref to it
            event, chan = await actor_nursery._actor.wait_for_peer(
                subactor.uid)
            portal = Portal(chan)
            actor_nursery._children[subactor.uid] = (subactor, proc, portal)

            # unblock parent task
            task_status.started(portal)

            # wait for ActorNursery.wait() to be called
            # this is required to ensure synchronization
            # with startup and registration of this actor in
            # ActorNursery.run_in_actor()
            await actor_nursery._join_procs.wait()

            if portal in actor_nursery._cancel_after_result_on_exit:
                cancel_scope = await nursery.start(
                    cancel_on_completion, portal, subactor, errors)

            # TODO: timeout block here?
            if proc.is_alive():
                await proc_waiter(proc)
            proc.join()

        log.debug(f"Joined {proc}")
        # pop child entry to indicate we are no longer managing this subactor
        subactor, proc, portal = actor_nursery._children.pop(subactor.uid)
        # cancel result waiter that may have been spawned in
        # tandem if not done already
        if cancel_scope:
            log.warning(
                f"Cancelling existing result waiter task for {subactor.uid}")
            cancel_scope.cancel()
