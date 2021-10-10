"""
Machinery for actor process spawning using multiple backends.
"""
from __future__ import annotations
import sys
import multiprocessing as mp
import platform
from typing import Any, Dict, Optional

import trio
from trio_typing import TaskStatus

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
from ._state import (
    current_actor,
    is_main_process,
)

from .log import get_logger
from ._portal import Portal
from ._actor import Actor
from ._entry import _mp_main
from ._exceptions import ActorFailure, RemoteActorError
from ._debug import maybe_wait_for_debugger


log = get_logger('tractor')

# placeholder for an mp start context if so using that backend
_ctx: Optional[mp.context.BaseContext] = None
_spawn_method: str = "trio"


if platform.system() == 'Windows':

    _ctx = mp.get_context("spawn")

    async def proc_waiter(proc: mp.Process) -> None:
        await trio.lowlevel.WaitForSingleObject(proc.sentinel)
else:
    # *NIX systems use ``trio`` primitives as our default as well

    async def proc_waiter(proc: mp.Process) -> None:
        await trio.lowlevel.wait_readable(proc.sentinel)


def try_set_start_method(name: str) -> Optional[mp.context.BaseContext]:
    """Attempt to set the method for process starting, aka the "actor
    spawning backend".

    If the desired method is not supported this function will error.
    On Windows only the ``multiprocessing`` "spawn" method is offered
    besides the default ``trio`` which uses async wrapping around
    ``subprocess.Popen``.
    """
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
        _forkserver_override.override_stdlib()
        _ctx = mp.get_context(name)
    elif name == 'trio':
        _ctx = None
    else:
        _ctx = mp.get_context(name)

    _spawn_method = name
    return _ctx


async def result_from_portal(
    portal: Portal,
    actor: Actor,

    errors: Dict[Tuple[str, str], Exception],
    cancel_on_result: bool = False,
    task_status: TaskStatus[trio.CancelScope] = trio.TASK_STATUS_IGNORED,

) -> None:
    """
    Cancel actor gracefully once it's "main" portal's
    result arrives.

    Should only be called for actors spawned with `run_in_actor()`.

    """
    __tracebackhide__ = True

    uid = portal.channel.uid

    # cancel control is explicityl done by the caller
    with trio.CancelScope() as cs:
        task_status.started(cs)

        # if this call errors we store the exception for later
        # in ``errors`` which will be reraised inside
        # a MultiError and we still send out a cancel request
        # result = await exhaust_portal(portal, actor)
        try:
            log.debug(f"Waiting on final result from {actor.uid}")

            # XXX: streams should never be reaped here since they should
            # always be established and shutdown using a context manager api
            result = await portal.result()
            log.debug(f"Returning final result: {result}")

        except (Exception, trio.MultiError) as err:
            # we reraise in the parent task via a ``trio.MultiError``
            result = err
            errors[actor.uid] = err
            raise

        except trio.Cancelled as err:
            # lol, of course we need this too ;P
            # TODO: merge with above?
            log.warning(f"Cancelled `Portal.result()` waiter for {uid}")
            result = err
            # errors[actor.uid] = err
            # raise

        if cancel_on_result:
            if isinstance(result, Exception):
                # errors[actor.uid] = result
                log.warning(
                    f"Cancelling single-task-run {uid} after error {result}"
                )
                # raise result

            else:
                log.runtime(
                    f"Cancelling {uid} gracefully "
                    f"after one-time-task result {result}")

            # an actor that was `.run_in_actor()` executes a single task
            # and delivers the result, then we cancel it.
            # TODO: likely in the future we should just implement this using
            # the new `open_context()` IPC api, since it's the more general
            # api and can represent this form.
            # XXX: do we need this?
            # await maybe_wait_for_debugger()
            await portal.cancel_actor()

        return result


async def do_hard_kill(

    proc: trio.Process,
    timeout: float,

) -> None:
    '''
    Hard kill a process with timeout.

    '''
    log.debug(f"Hard killing {proc}")
    # NOTE: this timeout used to do nothing since we were shielding
    # the ``.wait()`` inside ``new_proc()`` which will pretty much
    # never release until the process exits, now it acts as
    # a hard-kill time ultimatum.
    with trio.move_on_after(timeout) as cs:

        # NOTE: This ``__aexit__()`` shields internally and originally
        # would tear down stdstreams via ``trio.Process.aclose()``.
        async with proc:
            log.debug(f"Terminating {proc}")

    if cs.cancelled_caught:

        # this is a "softer" kill that we should probably use
        # eventually and let the zombie lord do the `.kill()`
        # proc.terminate()

        # XXX: should pretty much never get here unless we have
        # to move the bits from ``proc.__aexit__()`` out and
        # into here.
        log.critical(f"{timeout} timeout, HARD KILLING {proc}")
        proc.kill()


async def reap_proc(

    proc: trio.Process,
    terminate_after: float = float('inf'),
    hard_kill_after: int = 0.1,

) -> None:
    with trio.move_on_after(terminate_after) as cs:
        # Wait for proc termination but **dont' yet** do
        # any out-of-ipc-land termination / process
        # killing. This is a "light" (cancellable) join,
        # the hard join is below after timeout
        await proc.wait()

    if cs.cancelled_caught and terminate_after is not float('inf'):
        # Always "hard" join lingering sub procs since no
        # actor zombies are allowed!
        log.warning(
            # f'Failed to gracefully terminate {subactor.uid}')
            f'Failed to gracefully terminate {proc}\n'
            f"Attempting to hard kill {proc}")

        with trio.CancelScope(shield=True):
            # XXX: do this **after**
            # cancellation/tearfown to avoid killing the
            # process too early since trio does this
            # internally on ``__aexit__()``
            await do_hard_kill(proc, hard_kill_after)


async def new_proc(

    name: str,
    actor_nursery: 'ActorNursery',  # type: ignore  # noqa
    subactor: Actor,
    errors: Dict[Tuple[str, str], Exception],

    # passed through to actor main
    bind_addr: Tuple[str, int],
    parent_addr: Tuple[str, int],
    _runtime_vars: Dict[str, Any],  # serialized and sent to _child
    *,

    graceful_kill_timeout: int = 3,
    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED

) -> None:
    """
    Create a new ``multiprocessing.Process`` using the
    spawn method as configured using ``try_set_start_method()``.

    """
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
            str(uid),
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

        proc = await trio.open_process(spawn_cmd)

        log.info(f"Started {proc}")
        portal: Optional[Portal] = None

        # handle cancellation during child connect-back, kill
        # any cancelled spawn sequence immediately.
        try:
            # wait for actor to spawn and connect back to us
            # channel should have handshake completed by the
            # local actor by the time we get a ref to it
            event, chan = await actor_nursery._actor.wait_for_peer(
                subactor.uid)

        except trio.Cancelled:
            # reap un-contacted process which are started
            # but never setup a connection to parent.
            log.warning(f'Spawning aborted due to cancel {proc}')
            with trio.CancelScope(shield=True):
                await do_hard_kill(proc, 0.1)

            # TODO: should we have a custom error for this maybe derived
            # from ``subprocess``?
            raise

        # the child successfully connected back to us.
        actor_nursery_cancel_called = None
        portal = Portal(chan)
        actor_nursery._children[subactor.uid] = (
            subactor, proc, portal)

        # track child in current nursery
        curr_actor = current_actor()
        curr_actor._actoruid2nursery[subactor.uid] = actor_nursery

        try:
            # send additional init params
            await chan.send({
                "_parent_main_data": subactor._parent_main_data,
                "enable_modules": subactor.enable_modules,
                "_arb_addr": subactor._arb_addr,
                "bind_host": bind_addr[0],
                "bind_port": bind_addr[1],
                "_runtime_vars": _runtime_vars,
            })

            # resume caller at next checkpoint now that child is up
            task_status.started(portal)

            # this either completes or is cancelled and should only
            # **and always** be set once the actor nursery has errored
            # or exitted.
            with trio.CancelScope(shield=True):
                await actor_nursery._join_procs.wait()

        except (
            BaseException
            # trio.Cancelled,
            # KeyboardInterrupt,
            # trio.MultiError,
            # RuntimeError,
        ) as cerr:

            log.exception(f'Relaying unexpected {cerr} to nursery')

            # sending IPC-msg level cancel requests is expected to be
            # managed by the nursery.
            with trio.CancelScope(shield=True):
                await actor_nursery._handle_err(err, portal=portal)

            if portal.channel.connected():
                if ria:
                    # this may raise which we want right?
                    await result_from_portal(
                        portal,
                        subactor,
                        errors,
                        # True,  # cancel_on_result
                    )

        finally:
            # 2 cases:
            # - actor nursery was cancelled in which case
            #   we want to try a soft reap of the actor via
            #   ipc cancellation and then failing that do a hard
            #   reap.
            # - this is normal termination and we must wait indefinitely
            #   for ria and daemon actors
            reaping_cancelled: bool = False
            ria = portal in actor_nursery._cancel_after_result_on_exit

            # this is the soft reap sequence. we can
            # either collect results:
            # - ria actors get them them via ``Portal.result()``
            # - we wait forever on daemon actors until they're
            #   cancelled by user code via ``Portal.cancel_actor()``
            #   or ``ActorNursery.cancel(). in the latter case
            #   we have to expect another cancel here since
            #   the task spawning nurseries will both be cacelled
            #   by ``ActorNursery.cancel()``.

            # OR, we're cancelled while collecting results, which
            # case we need to try another soft cancel and reap  attempt.
            try:
                log.cancel(f'Starting soft actor reap for {uid}')
                cancel_scope = None
                # async with trio.open_nursery() as nursery:

                if portal.channel.connected() and ria:

                    # we wait for result and cancel on completion
                    await result_from_portal(
                        portal,
                        subactor,
                        errors,
                        True,  # cancel_on_result
                    )
                        # # collect any expected ``.run_in_actor()`` results
                        # cancel_scope = await nursery.start(
                        #     result_from_portal,
                        #     portal,
                        #     subactor,
                        #     errors,
                        #     True,  # cancel_on_result
                        # )

                # soft & cancellable
                await reap_proc(proc)

                    # # if proc terminates before portal result
                    # if cancel_scope:
                    #     cancel_scope.cancel()

            except (
                RemoteActorError,
            ) as err:
                reaping_cancelled = err
                log.exception(f'{uid} remote error')
                await actor_nursery._handle_err(err, portal=portal)

            except (
                trio.Cancelled,
            ) as err:
                reaping_cancelled = err
                if actor_nursery.cancelled:
                    log.cancel(f'{uid} wait cancelled by nursery')
                else:
                    log.exception(f'{uid} soft wait error?')

            except (
                BaseException
            ) as err:
                reaping_cancelled = err
                log.exception(f'{uid} soft reap local error')

            finally:
                if reaping_cancelled:
                    if actor_nursery.cancelled:
                        log.cancel(f'Nursery cancelled during soft wait for {uid}')

                with trio.CancelScope(shield=True):
                    await maybe_wait_for_debugger()

                    # XXX: can't do this, it'll hang some tests.. no
                    # idea why yet.
                    # with trio.CancelScope(shield=True):
                    #     await actor_nursery._handle_err(
                    #         reaping_cancelled,
                    #         portal=portal
                    #     )

                # hard reap sequence with timeouts
                if proc.poll() is None:
                    log.cancel(f'Attempting hard reap for {uid}')

                    with trio.CancelScope(shield=True):

                        # hard reap sequence
                        # ``Portal.cancel_actor()`` is expected to have
                        # been called by the supervising nursery so we
                        # do **not** call it here.

                        await reap_proc(
                            proc,
                            # this is the same as previous timeout
                            # setting before rewriting this spawn
                            # section
                            terminate_after=3,
                        )


                # if somehow the hard reap didn't collect the child then
                # we send in the big gunz.
                while proc.poll() is None:
                    log.critical(
                        f'ZOMBIE LORD HAS ARRIVED for your {uid}:\n'
                        f'{proc}'
                    )
                    with trio.CancelScope(shield=True):
                        await reap_proc(
                            proc,
                            terminate_after=0.1,
                        )

                log.info(f"Joined {proc}")

                # 2 cases:
                # - the actor terminated gracefully
                # - we're cancelled and likely need to re-raise

                # pop child entry to indicate we no longer managing this
                # subactor
                subactor, proc, portal = actor_nursery._children.pop(
                    subactor.uid)
                if not actor_nursery._children:
                    log.cancel(f"{uid} reports all children complete!")
                    actor_nursery._all_children_reaped.set()

                # not entirely sure why we need this.. but without it
                # the reaping cancelled error is never reported upwards
                # to the spawn nursery?
                if reaping_cancelled:
                    raise reaping_cancelled

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
    errors: Dict[Tuple[str, str], Exception],
    # passed through to actor main
    bind_addr: Tuple[str, int],
    parent_addr: Tuple[str, int],
    _runtime_vars: Dict[str, Any],  # serialized and sent to _child
    *,
    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED

) -> None:
    async with trio.open_nursery() as nursery:
        assert _ctx
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

            # no shield is required here (vs. above on the trio backend)
            # since debug mode is not supported on mp.
            await actor_nursery._join_procs.wait()

        finally:
            # XXX: in the case we were cancelled before the sub-proc
            # registered itself back we must be sure to try and clean
            # any process we may have started.

            reaping_cancelled: bool = False
            cancel_scope: Optional[trio.CancelScope] = None
            cancel_exc: Optional[trio.Cancelled] = None

            if portal in actor_nursery._cancel_after_result_on_exit:
                try:
                    # async with trio.open_nursery() as n:
                    # n.cancel_scope.shield = True
                    cancel_scope = await nursery.start(
                        result_from_portal,
                        portal,
                        subactor,
                        errors
                    )
                except trio.Cancelled as err:
                    cancel_exc = err

                    # if the reaping task was cancelled we may have hit
                    # a race where the subproc disconnected before we
                    # could send it a message to cancel (classic 2 generals)
                    # in that case, wait shortly then kill the process.
                    reaping_cancelled = True

                    if proc.is_alive():
                        with trio.move_on_after(0.1) as cs:
                            cs.shield = True
                            await proc_waiter(proc)

                        if cs.cancelled_caught:
                            proc.terminate()

            if not reaping_cancelled and proc.is_alive():
                await proc_waiter(proc)

            # TODO: timeout block here?
            proc.join()

            log.debug(f"Joined {proc}")
            # pop child entry to indicate we are no longer managing subactor
            subactor, proc, portal = actor_nursery._children.pop(subactor.uid)

            # cancel result waiter that may have been spawned in
            # tandem if not done already
            if cancel_scope:
                log.warning(
                    "Cancelling existing result waiter task for "
                    f"{subactor.uid}")
                cancel_scope.cancel()

            elif reaping_cancelled:  # let the cancellation bubble up
                assert cancel_exc
                raise cancel_exc
