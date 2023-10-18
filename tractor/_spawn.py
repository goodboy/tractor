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
import multiprocessing as mp
import sys
import platform
from typing import (
    Any,
    Awaitable,
    Literal,
    Callable,
    TypeVar,
    TYPE_CHECKING,
)

import trio
from trio import TaskStatus

from .devx._debug import (
    maybe_wait_for_debugger,
    acquire_debug_lock,
)
from tractor._state import (
    current_actor,
    is_main_process,
    is_root_process,
    debug_mode,
)
from tractor.log import get_logger
from tractor._portal import Portal
from tractor._runtime import Actor
from tractor._entry import _mp_main
from tractor._exceptions import ActorFailure


if TYPE_CHECKING:
    from ._supervise import ActorNursery
    ProcessType = TypeVar('ProcessType', mp.Process, trio.Process)

log = get_logger('tractor')

# placeholder for an mp start context if so using that backend
_ctx: mp.context.BaseContext | None = None
SpawnMethodKey = Literal[
    'trio',  # supported on all platforms
    'mp_spawn',
    'mp_forkserver',  # posix only
]
_spawn_method: SpawnMethodKey = 'trio'


if platform.system() == 'Windows':

    _ctx = mp.get_context("spawn")

    async def proc_waiter(proc: mp.Process) -> None:
        await trio.lowlevel.WaitForSingleObject(proc.sentinel)
else:
    # *NIX systems use ``trio`` primitives as our default as well

    async def proc_waiter(proc: mp.Process) -> None:
        await trio.lowlevel.wait_readable(proc.sentinel)


def try_set_start_method(
    key: SpawnMethodKey

) -> mp.context.BaseContext | None:
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

    mp_methods = mp.get_all_start_methods()
    if 'fork' in mp_methods:
        # forking is incompatible with ``trio``s global task tree
        mp_methods.remove('fork')

    match key:
        case 'mp_forkserver':
            from . import _forkserver_override
            _forkserver_override.override_stdlib()
            _ctx = mp.get_context('forkserver')

        case 'mp_spawn':
            _ctx = mp.get_context('spawn')

        case 'trio':
            _ctx = None

        case _:
            raise ValueError(
                f'Spawn method `{key}` is invalid!\n'
                f'Please choose one of {SpawnMethodKey}'
            )

    _spawn_method = key
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
    __tracebackhide__ = True
    try:
        log.debug(f"Waiting on final result from {actor.uid}")

        # XXX: streams should never be reaped here since they should
        # always be established and shutdown using a context manager api
        final: Any = await portal.result()

    except (
        Exception,
        BaseExceptionGroup,
    ) as err:
        # we reraise in the parent task via a ``BaseExceptionGroup``
        return err

    except trio.Cancelled as err:
        # lol, of course we need this too ;P
        # TODO: merge with above?
        log.warning(
            'Cancelled portal result waiter task:\n'
            f'uid: {portal.channel.uid}\n'
            f'error: {err}\n'
        )
        return err

    else:
        log.debug(
            f'Returning final result from portal:\n'
            f'uid: {portal.channel.uid}\n'
            f'result: {final}\n'
        )
        return final


async def cancel_on_completion(

    portal: Portal,
    actor: Actor,
    errors: dict[tuple[str, str], Exception],

) -> None:
    '''
    Cancel actor gracefully once its "main" portal's
    result arrives.

    Should only be called for actors spawned via the
    `Portal.run_in_actor()` API.

    => and really this API will be deprecated and should be
    re-implemented as a `.hilevel.one_shot_task_nursery()`..)

    '''
    # if this call errors we store the exception for later
    # in ``errors`` which will be reraised inside
    # an exception group and we still send out a cancel request
    result: Any|Exception = await exhaust_portal(portal, actor)
    if isinstance(result, Exception):
        errors[actor.uid]: Exception = result
        log.cancel(
            'Cancelling subactor runtime due to error:\n\n'
            f'Portal.cancel_actor() => {portal.channel.uid}\n\n'
            f'error: {result}\n'
        )

    else:
        log.runtime(
            'Cancelling subactor gracefully:\n\n'
            f'Portal.cancel_actor() => {portal.channel.uid}\n\n'
            f'result: {result}\n'
        )

    # cancel the process now that we have a final result
    await portal.cancel_actor()


async def hard_kill(
    proc: trio.Process,
    terminate_after: int = 1.6,

    # NOTE: for mucking with `.pause()`-ing inside the runtime
    # whilst also hacking on it XD
    # terminate_after: int = 99999,

    # NOTE: for mucking with `.pause()`-ing inside the runtime
    # whilst also hacking on it XD
    # terminate_after: int = 99999,

) -> None:
    '''
    Un-gracefully terminate an OS level `trio.Process` after timeout.

    Used in 2 main cases:

    - "unknown remote runtime state": a hanging/stalled actor that
      isn't responding after sending a (graceful) runtime cancel
      request via an IPC msg.
    - "cancelled during spawn": a process who's actor runtime was
      cancelled before full startup completed (such that
      cancel-request-handling machinery was never fully
      initialized) and thus a "cancel request msg" is never going
      to be handled.

    '''
    log.cancel(
        'Terminating sub-proc:\n'
        f'|_{proc}\n'
    )
    # NOTE: this timeout used to do nothing since we were shielding
    # the ``.wait()`` inside ``new_proc()`` which will pretty much
    # never release until the process exits, now it acts as
    # a hard-kill time ultimatum.
    with trio.move_on_after(terminate_after) as cs:

        # NOTE: code below was copied verbatim from the now deprecated
        # (in 0.20.0) ``trio._subrocess.Process.aclose()``, orig doc
        # string:
        #
        # Close any pipes we have to the process (both input and output)
        # and wait for it to exit. If cancelled, kills the process and
        # waits for it to finish exiting before propagating the
        # cancellation.
        #
        # This code was originally triggred by ``proc.__aexit__()``
        # but now must be called manually.
        with trio.CancelScope(shield=True):
            if proc.stdin is not None:
                await proc.stdin.aclose()
            if proc.stdout is not None:
                await proc.stdout.aclose()
            if proc.stderr is not None:
                await proc.stderr.aclose()
        try:
            await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                with trio.CancelScope(shield=True):
                    await proc.wait()

    # XXX NOTE XXX: zombie squad dispatch:
    # (should ideally never, but) If we do get here it means
    # graceful termination of a process failed and we need to
    # resort to OS level signalling to interrupt and cancel the
    # (presumably stalled or hung) actor. Since we never allow
    # zombies (as a feature) we ask the OS to do send in the
    # removal swad as the last resort.
    if cs.cancelled_caught:
        # TODO: toss in the skynet-logo face as ascii art?
        log.critical(
            # 'Well, the #ZOMBIE_LORD_IS_HERE# to collect\n'
            '#T-800 deployed to collect zombie B0\n'
            f'|\n'
            f'|_{proc}\n'
        )
        proc.kill()


async def soft_kill(

    proc: ProcessType,
    wait_func: Callable[
        [ProcessType],
        Awaitable,
    ],
    portal: Portal,

) -> None:
    '''
    Wait for proc termination but **don't yet** teardown
    std-streams since it will clobber any ongoing pdb REPL
    session.

    This is our "soft"/graceful, and thus itself also cancellable,
    join/reap on an actor-runtime-in-process shutdown; it is
    **not** the same as a "hard kill" via an OS signal (for that
    see `.hard_kill()`).

    '''
    uid: tuple[str, str] = portal.channel.uid
    try:
        log.cancel(
            'Soft killing sub-actor via `Portal.cancel_actor()`\n'
            f'|_{proc}\n'
        )
        # wait on sub-proc to signal termination
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
                "Cancel-the-cancel" request: if we detect that the
                underlying sub-process exited prior to
                a `Portal.cancel_actor()` call completing .

                '''
                await wait_func(proc)
                n.cancel_scope.cancel()

            # start a task to wait on the termination of the
            # process by itself waiting on a (caller provided) wait
            # function which should unblock when the target process
            # has terminated.
            n.start_soon(cancel_on_proc_deth)

            # send the actor-runtime a cancel request.
            await portal.cancel_actor()

            if proc.poll() is None:  # type: ignore
                log.warning(
                    'Subactor still alive after cancel request?\n\n'
                    f'uid: {uid}\n'
                    f'|_{proc}\n'
                )
                n.cancel_scope.cancel()
        raise


async def new_proc(
    name: str,
    actor_nursery: ActorNursery,
    subactor: Actor,
    errors: dict[tuple[str, str], Exception],

    # passed through to actor main
    bind_addrs: list[tuple[str, int]],
    parent_addr: tuple[str, int],
    _runtime_vars: dict[str, Any],  # serialized and sent to _child

    *,

    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED

) -> None:

    # lookup backend spawning target
    target: Callable = _methods[_spawn_method]

    # mark the new actor with the global spawn method
    subactor._spawn_method = _spawn_method

    await target(
        name,
        actor_nursery,
        subactor,
        errors,
        bind_addrs,
        parent_addr,
        _runtime_vars,  # run time vars
        infect_asyncio=infect_asyncio,
        task_status=task_status,
    )


async def trio_proc(
    name: str,
    actor_nursery: ActorNursery,
    subactor: Actor,
    errors: dict[tuple[str, str], Exception],

    # passed through to actor main
    bind_addrs: list[tuple[str, int]],
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
    proc: trio.Process|None = None
    try:
        try:
            # TODO: needs ``trio_typing`` patch?
            proc = await trio.lowlevel.open_process(spawn_cmd)
            log.runtime(
                'Started new sub-proc\n'
                f'|_{proc}\n'
            )

            # wait for actor to spawn and connect back to us
            # channel should have handshake completed by the
            # local actor by the time we get a ref to it
            event, chan = await actor_nursery._actor.wait_for_peer(
                subactor.uid
            )

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
                        async with acquire_debug_lock(subactor.uid):
                            # soft wait on the proc to terminate
                            with trio.move_on_after(0.5):
                                await proc.wait()
            raise

        # a sub-proc ref **must** exist now
        assert proc

        portal = Portal(chan)
        actor_nursery._children[subactor.uid] = (
            subactor,
            proc,
            portal,
        )

        # send additional init params
        await chan.send({
            '_parent_main_data': subactor._parent_main_data,
            'enable_modules': subactor.enable_modules,
            '_reg_addrs': subactor._reg_addrs,
            'bind_addrs': bind_addrs,
            '_runtime_vars': _runtime_vars,
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
            await soft_kill(
                proc,
                trio.Process.wait,
                portal
            )

            # cancel result waiter that may have been spawned in
            # tandem if not done already
            log.cancel(
                'Cancelling existing result waiter task for '
                f'{subactor.uid}'
            )
            nursery.cancel_scope.cancel()

    finally:
        # XXX NOTE XXX: The "hard" reap since no actor zombies are
        # allowed! Do this **after** cancellation/teardown to avoid
        # killing the process too early.
        if proc:
            log.cancel(f'Hard reap sequence starting for {subactor.uid}')
            with trio.CancelScope(shield=True):

                # don't clobber an ongoing pdb
                if cancelled_during_spawn:
                    # Try again to avoid TTY clobbering.
                    async with acquire_debug_lock(subactor.uid):
                        with trio.move_on_after(0.5):
                            await proc.wait()

                await maybe_wait_for_debugger(
                    child_in_debug=_runtime_vars.get(
                        '_debug_mode', False
                    ),
                    header_msg=(
                        'Delaying subproc reaper while debugger locked..\n'
                    ),

                    # TODO: need a diff value then default?
                    # poll_steps=9999999,
                )
                # TODO: solve the following issue where we need
                # to do a similar wait like this but in an
                # "intermediary" parent actor that itself isn't
                # in debug but has a child that is, and we need
                # to hold off on relaying SIGINT until that child
                # is complete.
                # https://github.com/goodboy/tractor/issues/320
                # -[ ] we need to handle non-root parent-actors specially
                # by somehow determining if a child is in debug and then
                # avoiding cancel/kill of said child by this
                # (intermediary) parent until such a time as the root says
                # the pdb lock is released and we are good to tear down
                # (our children)..
                #
                # -[ ] so maybe something like this where we try to
                #     acquire the lock and get notified of who has it,
                #     check that uid against our known children?
                # this_uid: tuple[str, str] = current_actor().uid
                # await acquire_debug_lock(this_uid)

                if proc.poll() is None:
                    log.cancel(f"Attempting to hard kill {proc}")
                    await hard_kill(proc)

                log.debug(f"Joined {proc}")
        else:
            log.warning('Nursery cancelled before sub-proc started')

        if not cancelled_during_spawn:
            # pop child entry to indicate we no longer managing this
            # subactor
            actor_nursery._children.pop(subactor.uid)


async def mp_proc(
    name: str,
    actor_nursery: ActorNursery,  # type: ignore  # noqa
    subactor: Actor,
    errors: dict[tuple[str, str], Exception],
    # passed through to actor main
    bind_addrs: list[tuple[str, int]],
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

    proc: mp.Process = _ctx.Process(  # type: ignore
        target=_mp_main,
        args=(
            subactor,
            bind_addrs,
            fs_info,
            _spawn_method,
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
            await soft_kill(
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
        actor_nursery._children.pop(subactor.uid)

        # TODO: prolly report to ``mypy`` how this causes all sorts of
        # false errors..
        # subactor, proc, portal = actor_nursery._children.pop(subactor.uid)


# proc spawning backend target map
_methods: dict[SpawnMethodKey, Callable] = {
    'trio': trio_proc,
    'mp_spawn': mp_proc,
    'mp_forkserver': mp_proc,
}
