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
Top level routines & machinery for actor-as-process/subint spawning
over multiple backends.

"""
from __future__ import annotations
import multiprocessing as mp
import platform
import sys
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

from ..devx import debug
from tractor.runtime._state import (
    _runtime_vars,
)
from tractor.log import get_logger
from tractor.discovery._addr import UnwrappedAddress
from tractor.runtime._portal import Portal
from tractor.runtime._runtime import Actor
from tractor.msg import types as msgtypes


if TYPE_CHECKING:
    from tractor.ipc import (
        Channel,
    )
    from tractor.runtime._supervise import ActorNursery
    ProcessType = TypeVar('ProcessType', mp.Process, trio.Process)


log = get_logger('tractor')

# placeholder for an mp start context if so using that backend
_ctx: mp.context.BaseContext | None = None
SpawnMethodKey = Literal[
    'trio',  # supported on all platforms
    'mp_spawn',
    'mp_forkserver',  # posix only
    'subint',  # py3.14+ via `concurrent.interpreters` (PEP 734)
    # EXPERIMENTAL — blocked at the CPython level. The
    # design goal was a `trio+fork`-safe subproc spawn via
    # `os.fork()` from a trio-free launchpad sub-interpreter,
    # but CPython's `PyOS_AfterFork_Child` → `_PyInterpreterState_DeleteExceptMain`
    # requires fork come from the main interp. See
    # `tractor.spawn._subint_fork` +
    # `ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md`
    # + issue #379 for the full analysis.
    'subint_fork',
    # EXPERIMENTAL — the `subint_fork` workaround. `os.fork()`
    # from a non-trio worker thread (never entered a subint)
    # is CPython-legal and works cleanly; forked child runs
    # `tractor._child._actor_child_main()` against a trio
    # runtime, exactly like `trio_proc` but via fork instead
    # of subproc-exec. See `tractor.spawn._subint_forkserver`.
    'subint_forkserver',
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

        case 'subint' | 'subint_fork' | 'subint_forkserver':
            # All subint-family backends need no `mp.context`;
            # all three feature-gate on the py3.14 public
            # `concurrent.interpreters` wrapper (PEP 734). See
            # `tractor.spawn._subint` for the detailed
            # reasoning. `subint_fork` is blocked at the
            # CPython level (raises `NotImplementedError`);
            # `subint_forkserver` is the working workaround.
            from ._subint import _has_subints
            if not _has_subints:
                raise RuntimeError(
                    f'Spawn method {key!r} requires Python 3.14+.\n'
                    f'(On py3.13 the private `_interpreters` C '
                    f'module exists but tractor\'s spawn flow '
                    f'wedges — see `tractor.spawn._subint` '
                    f'docstring for details.)\n'
                    f'Current runtime: {sys.version}'
                )
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
        log.debug(
            f'Waiting on final result from {actor.aid.uid}'
        )

        # XXX: streams should never be reaped here since they should
        # always be established and shutdown using a context manager api
        final: Any = await portal.wait_for_result()

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
            f'uid: {portal.channel.aid}\n'
            f'error: {err}\n'
        )
        return err

    else:
        log.debug(
            f'Returning final result from portal:\n'
            f'uid: {portal.channel.aid}\n'
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
    result: Any|Exception = await exhaust_portal(
        portal,
        actor,
    )
    if isinstance(result, Exception):
        errors[actor.aid.uid]: Exception = result
        log.cancel(
            'Cancelling subactor runtime due to error:\n\n'
            f'Portal.cancel_actor() => {portal.channel.aid}\n\n'
            f'error: {result}\n'
        )

    else:
        log.runtime(
            'Cancelling subactor gracefully:\n\n'
            f'Portal.cancel_actor() => {portal.channel.aid}\n\n'
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
        'Terminating sub-proc\n'
        f'>x)\n'
        f' |_{proc}\n'
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

        # TODO? attempt at intermediary-rent-sub
        # with child in debug lock?
        # |_https://github.com/goodboy/tractor/issues/320
        #
        # if not is_root_process():
        #     log.warning(
        #         'Attempting to acquire debug-REPL-lock before zombie reap!'
        #     )
        #     with trio.CancelScope(shield=True):
        #         async with debug.acquire_debug_lock(
        #             subactor_uid=current_actor().aid.uid,
        #         ) as _ctx:
        #             log.warning(
        #                 'Acquired debug lock, child ready to be killed ??\n'
        #             )

        # TODO: toss in the skynet-logo face as ascii art?
        log.critical(
            # 'Well, the #ZOMBIE_LORD_IS_HERE# to collect\n'
            '#T-800 deployed to collect zombie B0\n'
            f'>x)\n'
            f' |_{proc}\n'
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
    chan: Channel = portal.channel
    peer_aid: msgtypes.Aid = chan.aid
    try:
        log.cancel(
            f'Soft killing sub-actor via portal request\n'
            f'\n'
            f'c)=> {peer_aid.reprol()}@[{chan.maddr}]\n'
            f'   |_{proc}\n'
        )
        # wait on sub-proc to signal termination
        await wait_func(proc)

    except trio.Cancelled:
        with trio.CancelScope(shield=True):
            await debug.maybe_wait_for_debugger(
                child_in_debug=_runtime_vars.get(
                    '_debug_mode', False
                ),
                header_msg=(
                    'Delaying `soft_kill()` subproc reaper while debugger locked..\n'
                ),
                # TODO: need a diff value then default?
                # poll_steps=9999999,
            )

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
                    f'uid: {peer_aid}\n'
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
    bind_addrs: list[UnwrappedAddress],
    parent_addr: UnwrappedAddress,
    _runtime_vars: dict[str, Any],  # serialized and sent to _child

    *,

    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED,
    proc_kwargs: dict[str, any] = {}

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
        proc_kwargs=proc_kwargs
    )


# NOTE: bottom-of-module to avoid a circular import since the
# backend submodules pull `cancel_on_completion`/`soft_kill`/
# `hard_kill`/`proc_waiter` from this module.
from ._trio import trio_proc
from ._mp import mp_proc
from ._subint import subint_proc
from ._subint_fork import subint_fork_proc
from ._subint_forkserver import subint_forkserver_proc


# proc spawning backend target map
_methods: dict[SpawnMethodKey, Callable] = {
    'trio': trio_proc,
    'mp_spawn': mp_proc,
    'mp_forkserver': mp_proc,
    'subint': subint_proc,
    # blocked at CPython level — see `_subint_fork.py` +
    # `ai/conc-anal/subint_fork_blocked_by_cpython_post_fork_issue.md`.
    # Kept here so `--spawn-backend=subint_fork` routes to a
    # clean `NotImplementedError` with pointer to the analysis,
    # rather than an "invalid backend" error.
    'subint_fork': subint_fork_proc,
    # WIP — fork-from-non-trio-worker-thread, works on py3.14+
    # (validated via `ai/conc-anal/subint_fork_from_main_thread_smoketest.py`).
    # See `tractor.spawn._subint_forkserver`.
    'subint_forkserver': subint_forkserver_proc,
}
