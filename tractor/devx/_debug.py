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
Multi-core debugging for da peeps!

"""
from __future__ import annotations
import bdb
import os
import sys
import signal
from functools import (
    partial,
    cached_property,
)
from contextlib import (
    asynccontextmanager as acm,
    contextmanager as cm,
    nullcontext,
)
from typing import (
    Any,
    Callable,
    AsyncIterator,
    AsyncGenerator,
)
from types import FrameType

import pdbp
import tractor
import trio
from trio_typing import (
    TaskStatus,
    # Task,
)

from ..log import get_logger
from .._state import (
    is_root_process,
    debug_mode,
)
from .._exceptions import (
    is_multi_cancelled,
    ContextCancelled,
)
from .._ipc import Channel

log = get_logger(__name__)


__all__ = [
    'breakpoint',
    'post_mortem',
]


class Lock:
    '''
    Actor global debug lock state.

    Mostly to avoid a lot of ``global`` declarations for now XD.

    '''
    repl: MultiActorPdb | None = None
    # placeholder for function to set a ``trio.Event`` on debugger exit
    # pdb_release_hook: Callable | None = None

    _trio_handler: Callable[
        [int, FrameType | None], Any
    ] | int | None = None

    # actor-wide variable pointing to current task name using debugger
    local_task_in_debug: str | None = None

    # NOTE: set by the current task waiting on the root tty lock from
    # the CALLER side of the `lock_tty_for_child()` context entry-call
    # and must be cancelled if this actor is cancelled via IPC
    # request-message otherwise deadlocks with the parent actor may
    # ensure
    _debugger_request_cs: trio.CancelScope | None = None

    # NOTE: set only in the root actor for the **local** root spawned task
    # which has acquired the lock (i.e. this is on the callee side of
    # the `lock_tty_for_child()` context entry).
    _root_local_task_cs_in_debug: trio.CancelScope | None = None

    # actor tree-wide actor uid that supposedly has the tty lock
    global_actor_in_debug: tuple[str, str] = None

    local_pdb_complete: trio.Event | None = None
    no_remote_has_tty: trio.Event | None = None

    # lock in root actor preventing multi-access to local tty
    _debug_lock: trio.StrictFIFOLock = trio.StrictFIFOLock()

    _orig_sigint_handler: Callable | None = None
    _blocked: set[tuple[str, str]] = set()

    @classmethod
    def shield_sigint(cls):
        cls._orig_sigint_handler = signal.signal(
            signal.SIGINT,
            shield_sigint_handler,
        )

    @classmethod
    @pdbp.hideframe  # XXX NOTE XXX see below in `.pause_from_sync()`
    def unshield_sigint(cls):
        # always restore ``trio``'s sigint handler. see notes below in
        # the pdb factory about the nightmare that is that code swapping
        # out the handler when the repl activates...
        signal.signal(signal.SIGINT, cls._trio_handler)
        cls._orig_sigint_handler = None

    @classmethod
    def release(cls):
        try:
            cls._debug_lock.release()
        except RuntimeError:
            # uhhh makes no sense but been seeing the non-owner
            # release error even though this is definitely the task
            # that locked?
            owner = cls._debug_lock.statistics().owner
            if owner:
                raise

        try:
            # sometimes the ``trio`` might already be terminated in
            # which case this call will raise.
            if cls.local_pdb_complete is not None:
                cls.local_pdb_complete.set()
        finally:
            # restore original sigint handler
            cls.unshield_sigint()
            cls.repl = None

            # actor-local state, irrelevant for non-root.
            cls.global_actor_in_debug = None
            cls.local_task_in_debug = None



class TractorConfig(pdbp.DefaultConfig):
    '''
    Custom ``pdbp`` goodness :surfer:

    '''
    use_pygments: bool = True
    sticky_by_default: bool = False
    enable_hidden_frames: bool = True

    # much thanks @mdmintz for the hot tip!
    # fixes line spacing issue when resizing terminal B)
    truncate_long_lines: bool = False


class MultiActorPdb(pdbp.Pdb):
    '''
    Add teardown hooks to the regular ``pdbp.Pdb``.

    '''
    # override the pdbp config with our coolio one
    DefaultConfig = TractorConfig

    # def preloop(self):
    #     print('IN PRELOOP')
    #     super().preloop()

    # TODO: figure out how to disallow recursive .set_trace() entry
    # since that'll cause deadlock for us.
    def set_continue(self):
        try:
            super().set_continue()
        finally:
            Lock.release()

    def set_quit(self):
        try:
            super().set_quit()
        finally:
            Lock.release()

    # XXX NOTE: we only override this because apparently the stdlib pdb
    # bois likes to touch the SIGINT handler as much as i like to touch
    # my d$%&.
    def _cmdloop(self):
        self.cmdloop()

    @cached_property
    def shname(self) -> str | None:
        '''
        Attempt to return the login shell name with a special check for
        the infamous `xonsh` since it seems to have some issues much
        different from std shells when it comes to flushing the prompt?

        '''
        # SUPER HACKY and only really works if `xonsh` is not used
        # before spawning further sub-shells..
        shpath = os.getenv('SHELL', None)

        if shpath:
            if (
                os.getenv('XONSH_LOGIN', default=False)
                or 'xonsh' in shpath
            ):
                return 'xonsh'

            return os.path.basename(shpath)

        return None


@acm
async def _acquire_debug_lock_from_root_task(
    uid: tuple[str, str]

) -> AsyncIterator[trio.StrictFIFOLock]:
    '''
    Acquire a root-actor local FIFO lock which tracks mutex access of
    the process tree's global debugger breakpoint.

    This lock avoids tty clobbering (by preventing multiple processes
    reading from stdstreams) and ensures multi-actor, sequential access
    to the ``pdb`` repl.

    '''
    task_name: str = trio.lowlevel.current_task().name
    we_acquired: bool = False

    log.runtime(
        f"Attempting to acquire TTY lock, remote task: {task_name}:{uid}"
    )
    try:
        log.runtime(
            f"entering lock checkpoint, remote task: {task_name}:{uid}"
        )
        # NOTE: if the surrounding cancel scope from the
        # `lock_tty_for_child()` caller is cancelled, this line should
        # unblock and NOT leave us in some kind of
        # a "child-locked-TTY-but-child-is-uncontactable-over-IPC"
        # condition.
        await Lock._debug_lock.acquire()
        we_acquired = True

        if Lock.no_remote_has_tty is None:
            # mark the tty lock as being in use so that the runtime
            # can try to avoid clobbering any connection from a child
            # that's currently relying on it.
            Lock.no_remote_has_tty = trio.Event()

        Lock.global_actor_in_debug = uid
        log.runtime(f"TTY lock acquired, remote task: {task_name}:{uid}")

        # NOTE: critical section: this yield is unshielded!

        # IF we received a cancel during the shielded lock entry of some
        # next-in-queue requesting task, then the resumption here will
        # result in that ``trio.Cancelled`` being raised to our caller
        # (likely from ``lock_tty_for_child()`` below)!  In
        # this case the ``finally:`` below should trigger and the
        # surrounding caller side context should cancel normally
        # relaying back to the caller.

        yield Lock._debug_lock

    finally:
        if (
            we_acquired
            and Lock._debug_lock.locked()
        ):
            Lock._debug_lock.release()

        # IFF there are no more requesting tasks queued up fire, the
        # "tty-unlocked" event thereby alerting any monitors of the lock that
        # we are now back in the "tty unlocked" state. This is basically
        # and edge triggered signal around an empty queue of sub-actor
        # tasks that may have tried to acquire the lock.
        stats = Lock._debug_lock.statistics()
        if (
            not stats.owner
        ):
            log.runtime(f"No more tasks waiting on tty lock! says {uid}")
            if Lock.no_remote_has_tty is not None:
                Lock.no_remote_has_tty.set()
                Lock.no_remote_has_tty = None

        Lock.global_actor_in_debug = None

        log.runtime(
            f"TTY lock released, remote task: {task_name}:{uid}"
        )


@tractor.context
async def lock_tty_for_child(

    ctx: tractor.Context,
    subactor_uid: tuple[str, str]

) -> str:
    '''
    Lock the TTY in the root process of an actor tree in a new
    inter-actor-context-task such that the ``pdbp`` debugger console
    can be mutex-allocated to the calling sub-actor for REPL control
    without interference by other processes / threads.

    NOTE: this task must be invoked in the root process of the actor
    tree. It is meant to be invoked as an rpc-task and should be
    highly reliable at releasing the mutex complete!

    '''
    task_name = trio.lowlevel.current_task().name

    if tuple(subactor_uid) in Lock._blocked:
        log.warning(
            f'Actor {subactor_uid} is blocked from acquiring debug lock\n'
            f"remote task: {task_name}:{subactor_uid}"
        )
        ctx._enter_debugger_on_cancel: bool = False
        await ctx.cancel(f'Debug lock blocked for {subactor_uid}')
        return 'pdb_lock_blocked'

    # TODO: when we get to true remote debugging
    # this will deliver stdin data?

    log.debug(
        "Attempting to acquire TTY lock\n"
        f"remote task: {task_name}:{subactor_uid}"
    )

    log.debug(f"Actor {subactor_uid} is WAITING on stdin hijack lock")
    Lock.shield_sigint()

    try:
        with (
            trio.CancelScope(shield=True) as debug_lock_cs,
        ):
            Lock._root_local_task_cs_in_debug = debug_lock_cs
            async with _acquire_debug_lock_from_root_task(subactor_uid):

                # indicate to child that we've locked stdio
                await ctx.started('Locked')
                log.debug(
                    f"Actor {subactor_uid} acquired stdin hijack lock"
                )

                # wait for unlock pdb by child
                async with ctx.open_stream() as stream:
                    assert await stream.receive() == 'pdb_unlock'

        return "pdb_unlock_complete"

    finally:
        Lock._root_local_task_cs_in_debug = None
        Lock.unshield_sigint()


async def wait_for_parent_stdin_hijack(
    actor_uid: tuple[str, str],
    task_status: TaskStatus[trio.CancelScope] = trio.TASK_STATUS_IGNORED
):
    '''
    Connect to the root actor via a ``Context`` and invoke a task which
    locks a root-local TTY lock: ``lock_tty_for_child()``; this func
    should be called in a new task from a child actor **and never the
    root*.

    This function is used by any sub-actor to acquire mutex access to
    the ``pdb`` REPL and thus the root's TTY for interactive debugging
    (see below inside ``pause()``). It can be used to ensure that
    an intermediate nursery-owning actor does not clobber its children
    if they are in debug (see below inside
    ``maybe_wait_for_debugger()``).

    '''
    from .._discovery import get_root

    with trio.CancelScope(shield=True) as cs:
        Lock._debugger_request_cs = cs

        try:
            async with get_root() as portal:

                # this syncs to child's ``Context.started()`` call.
                async with portal.open_context(

                    lock_tty_for_child,
                    subactor_uid=actor_uid,

                ) as (ctx, val):

                    log.debug('locked context')
                    assert val == 'Locked'

                    async with ctx.open_stream() as stream:
                        # unblock local caller

                        try:
                            assert Lock.local_pdb_complete
                            task_status.started(cs)
                            await Lock.local_pdb_complete.wait()

                        finally:
                            # TODO: shielding currently can cause hangs...
                            # with trio.CancelScope(shield=True):
                            await stream.send('pdb_unlock')

                        # sync with callee termination
                        assert await ctx.result() == "pdb_unlock_complete"

                log.debug('exitting child side locking task context')

        except ContextCancelled:
            log.warning('Root actor cancelled debug lock')
            raise

        finally:
            Lock.local_task_in_debug = None
            log.debug('Exiting debugger from child')


def mk_mpdb() -> tuple[MultiActorPdb, Callable]:

    pdb = MultiActorPdb()
    # signal.signal = pdbp.hideframe(signal.signal)

    Lock.shield_sigint()

    # XXX: These are the important flags mentioned in
    # https://github.com/python-trio/trio/issues/1155
    # which resolve the traceback spews to console.
    pdb.allow_kbdint = True
    pdb.nosigint = True

    return pdb, Lock.unshield_sigint


def shield_sigint_handler(
    signum: int,
    frame: 'frame',  # type: ignore # noqa
    # pdb_obj: MultiActorPdb | None = None,
    *args,

) -> None:
    '''
    Specialized, debugger-aware SIGINT handler.

    In childred we always ignore to avoid deadlocks since cancellation
    should always be managed by the parent supervising actor. The root
    is always cancelled on ctrl-c.

    '''
    __tracebackhide__ = True

    uid_in_debug: tuple[str, str] | None = Lock.global_actor_in_debug

    actor = tractor.current_actor()
    # print(f'{actor.uid} in HANDLER with ')

    def do_cancel():
        # If we haven't tried to cancel the runtime then do that instead
        # of raising a KBI (which may non-gracefully destroy
        # a ``trio.run()``).
        if not actor._cancel_called:
            actor.cancel_soon()

        # If the runtime is already cancelled it likely means the user
        # hit ctrl-c again because teardown didn't full take place in
        # which case we do the "hard" raising of a local KBI.
        else:
            raise KeyboardInterrupt

    any_connected: bool = False

    if uid_in_debug is not None:
        # try to see if the supposed (sub)actor in debug still
        # has an active connection to *this* actor, and if not
        # it's likely they aren't using the TTY lock / debugger
        # and we should propagate SIGINT normally.
        chans: list[tractor.Channel] = actor._peers.get(tuple(uid_in_debug))
        if chans:
            any_connected = any(chan.connected() for chan in chans)
            if not any_connected:
                log.warning(
                    'A global actor reported to be in debug '
                    'but no connection exists for this child:\n'
                    f'{uid_in_debug}\n'
                    'Allowing SIGINT propagation..'
                )
                return do_cancel()

    # only set in the actor actually running the REPL
    pdb_obj: MultiActorPdb | None = Lock.repl

    # root actor branch that reports whether or not a child
    # has locked debugger.
    if (
        is_root_process()
        and uid_in_debug is not None

        # XXX: only if there is an existing connection to the
        # (sub-)actor in debug do we ignore SIGINT in this
        # parent! Otherwise we may hang waiting for an actor
        # which has already terminated to unlock.
        and any_connected
    ):
        # we are root and some actor is in debug mode
        # if uid_in_debug is not None:

        if pdb_obj:
            name = uid_in_debug[0]
            if name != 'root':
                log.pdb(
                    f"Ignoring SIGINT, child in debug mode: `{uid_in_debug}`"
                )

            else:
                log.pdb(
                    "Ignoring SIGINT while in debug mode"
                )
    elif (
        is_root_process()
    ):
        if pdb_obj:
            log.pdb(
                "Ignoring SIGINT since debug mode is enabled"
            )

        if (
            Lock._root_local_task_cs_in_debug
            and not Lock._root_local_task_cs_in_debug.cancel_called
        ):
            Lock._root_local_task_cs_in_debug.cancel()

            # revert back to ``trio`` handler asap!
            Lock.unshield_sigint()

    # child actor that has locked the debugger
    elif not is_root_process():

        chan: Channel = actor._parent_chan
        if not chan or not chan.connected():
            log.warning(
                'A global actor reported to be in debug '
                'but no connection exists for its parent:\n'
                f'{uid_in_debug}\n'
                'Allowing SIGINT propagation..'
            )
            return do_cancel()

        task: str | None = Lock.local_task_in_debug
        if (
            task
            and pdb_obj
        ):
            log.pdb(
                f"Ignoring SIGINT while task in debug mode: `{task}`"
            )

        # TODO: how to handle the case of an intermediary-child actor
        # that **is not** marked in debug mode? See oustanding issue:
        # https://github.com/goodboy/tractor/issues/320
        # elif debug_mode():

    else:  # XXX: shouldn't ever get here?
        raise RuntimeError("WTFWTFWTF")
        # raise KeyboardInterrupt("WTFWTFWTF")

    # NOTE: currently (at least on ``fancycompleter`` 0.9.2)
    # it looks to be that the last command that was run (eg. ll)
    # will be repeated by default.

    # maybe redraw/print last REPL output to console since
    # we want to alert the user that more input is expect since
    # nothing has been done dur to ignoring sigint.
    if (
        pdb_obj  # only when this actor has a REPL engaged
    ):
        # XXX: yah, mega hack, but how else do we catch this madness XD
        if pdb_obj.shname == 'xonsh':
            pdb_obj.stdout.write(pdb_obj.prompt)

        pdb_obj.stdout.flush()

        # TODO: make this work like sticky mode where if there is output
        # detected as written to the tty we redraw this part underneath
        # and erase the past draw of this same bit above?
        # pdb_obj.sticky = True
        # pdb_obj._print_if_sticky()

        # also see these links for an approach from ``ptk``:
        # https://github.com/goodboy/tractor/issues/130#issuecomment-663752040
        # https://github.com/prompt-toolkit/python-prompt-toolkit/blob/c2c6af8a0308f9e5d7c0e28cb8a02963fe0ce07a/prompt_toolkit/patch_stdout.py


def _set_trace(
    actor: tractor.Actor | None = None,
    pdb: MultiActorPdb | None = None,
    shield: bool = False,
):
    __tracebackhide__: bool = True
    actor: tractor.Actor = actor or tractor.current_actor()

    # start 2 levels up in user code
    frame: FrameType | None = sys._getframe()
    if frame:
        frame: FrameType = frame.f_back  # type: ignore

    if (
        frame
        and (
            pdb
            and actor is not None
        ) or shield
    ):
        # pdbp.set_trace()
        log.pdb(f"\nAttaching pdb to actor: {actor.uid}\n")
        # no f!#$&* idea, but when we're in async land
        # we need 2x frames up?
        frame = frame.f_back
        # frame = frame.f_back

        # if shield:
        #     frame = frame.f_back

    else:
        pdb, undo_sigint = mk_mpdb()

        # we entered the global ``breakpoint()`` built-in from sync
        # code?
        Lock.local_task_in_debug = 'sync'

    pdb.set_trace(frame=frame)
    # undo_


async def pause(

    debug_func: Callable = _set_trace,
    release_lock_signal: trio.Event | None = None,

    # TODO: allow caller to pause despite task cancellation,
    # exactly the same as wrapping with:
    # with CancelScope(shield=True):
    #     await pause()
    # => the REMAINING ISSUE is that the scope's .__exit__() frame
    # is always show in the debugger on entry.. and there seems to
    # be no way to override it?..
    # shield: bool = False,

    # TODO:
    # shield: bool = False
    task_status: TaskStatus[trio.Event] = trio.TASK_STATUS_IGNORED

) -> None:
    '''
    A pause point (more commonly known as a "breakpoint") interrupt
    instruction for engaging a blocking debugger instance to
    conduct manual console-based-REPL-interaction from within
    `tractor`'s async runtime, normally from some single-threaded
    and currently executing actor-hosted-`trio`-task in some
    (remote) process.

    NOTE: we use the semantics "pause" since it better encompasses
    the entirety of the necessary global-runtime-state-mutation any
    actor-task must access and lock in order to get full isolated
    control over the process tree's root TTY:
    https://en.wikipedia.org/wiki/Breakpoint

    '''
    # __tracebackhide__ = True
    actor = tractor.current_actor()
    pdb, undo_sigint = mk_mpdb()
    task_name = trio.lowlevel.current_task().name

    if (
        not Lock.local_pdb_complete
        or Lock.local_pdb_complete.is_set()
    ):
        Lock.local_pdb_complete = trio.Event()

    # if shield:
    debug_func = partial(
        debug_func,
        # shield=shield,
    )

    # def _exit(self, *args, **kwargs):
    #     __tracebackhide__: bool = True
    #     super().__exit__(*args, **kwargs)

    # trio.CancelScope.__exit__.__tracebackhide__ = True

    # import types
    # with trio.CancelScope(shield=shield) as cs:
        # cs.__exit__ = types.MethodType(_exit, cs)
        # cs.__exit__.__tracebackhide__ = True

        # TODO: need a more robust check for the "root" actor
    if (
        not is_root_process()
        and actor._parent_chan  # a connected child
    ):

        if Lock.local_task_in_debug:

            # Recurrence entry case: this task already has the lock and
            # is likely recurrently entering a breakpoint
            if Lock.local_task_in_debug == task_name:
                # noop on recurrent entry case but we want to trigger
                # a checkpoint to allow other actors error-propagate and
                # potetially avoid infinite re-entries in some subactor.
                await trio.lowlevel.checkpoint()
                return

            # if **this** actor is already in debug mode block here
            # waiting for the control to be released - this allows
            # support for recursive entries to `tractor.breakpoint()`
            log.warning(f"{actor.uid} already has a debug lock, waiting...")

            await Lock.local_pdb_complete.wait()
            await trio.sleep(0.1)

        # mark local actor as "in debug mode" to avoid recurrent
        # entries/requests to the root process
        Lock.local_task_in_debug = task_name

        # this **must** be awaited by the caller and is done using the
        # root nursery so that the debugger can continue to run without
        # being restricted by the scope of a new task nursery.

        # TODO: if we want to debug a trio.Cancelled triggered exception
        # we have to figure out how to avoid having the service nursery
        # cancel on this task start? I *think* this works below:
        # ```python
        #   actor._service_n.cancel_scope.shield = shield
        # ```
        # but not entirely sure if that's a sane way to implement it?
        try:
            with trio.CancelScope(shield=True):
                await actor._service_n.start(
                    wait_for_parent_stdin_hijack,
                    actor.uid,
                )
                Lock.repl = pdb
        except RuntimeError:
            Lock.release()

            if actor._cancel_called:
                # service nursery won't be usable and we
                # don't want to lock up the root either way since
                # we're in (the midst of) cancellation.
                return

            raise

    elif is_root_process():

        # we also wait in the root-parent for any child that
        # may have the tty locked prior
        # TODO: wait, what about multiple root tasks acquiring it though?
        if Lock.global_actor_in_debug == actor.uid:
            # re-entrant root process already has it: noop.
            return

        # XXX: since we need to enter pdb synchronously below,
        # we have to release the lock manually from pdb completion
        # callbacks. Can't think of a nicer way then this atm.
        if Lock._debug_lock.locked():
            log.warning(
                'Root actor attempting to shield-acquire active tty lock'
                f' owned by {Lock.global_actor_in_debug}')

            # must shield here to avoid hitting a ``Cancelled`` and
            # a child getting stuck bc we clobbered the tty
            with trio.CancelScope(shield=True):
                await Lock._debug_lock.acquire()
        else:
            # may be cancelled
            await Lock._debug_lock.acquire()

        Lock.global_actor_in_debug = actor.uid
        Lock.local_task_in_debug = task_name
        Lock.repl = pdb

    try:
        if debug_func is None:
            # assert release_lock_signal, (
            #     'Must pass `release_lock_signal: trio.Event` if no '
            #     'trace func provided!'
            # )
            print(f"{actor.uid} ENTERING WAIT")
            task_status.started()

            # with trio.CancelScope(shield=True):
            #     await release_lock_signal.wait()

        else:
            # block here one (at the appropriate frame *up*) where
            # ``breakpoint()`` was awaited and begin handling stdio.
            log.debug("Entering the synchronous world of pdb")
            debug_func(actor, pdb)

    except bdb.BdbQuit:
        Lock.release()
        raise

    # XXX: apparently we can't do this without showing this frame
    # in the backtrace on first entry to the REPL? Seems like an odd
    # behaviour that should have been fixed by now. This is also why
    # we scrapped all the @cm approaches that were tried previously.
    # finally:
    #     __tracebackhide__ = True
    #     # frame = sys._getframe()
    #     # last_f = frame.f_back
    #     # last_f.f_globals['__tracebackhide__'] = True
    #     # signal.signal = pdbp.hideframe(signal.signal)


# TODO: allow pausing from sync code.
# normally by remapping python's builtin breakpoint() hook to this
# runtime aware version which takes care of all .
def pause_from_sync() -> None:
    print("ENTER SYNC PAUSE")
    actor: tractor.Actor = tractor.current_actor(
        err_on_no_runtime=False,
    )
    if actor:
        try:
            import greenback
            # __tracebackhide__ = True


            # task_can_release_tty_lock = trio.Event()

            # spawn bg task which will lock out the TTY, we poll
            # just below until the release event is reporting that task as
            # waiting.. not the most ideal but works for now ;)
            greenback.await_(
                actor._service_n.start(partial(
                    pause,
                    debug_func=None,
                    # release_lock_signal=task_can_release_tty_lock,
                ))
            )

        except ModuleNotFoundError:
            log.warning('NO GREENBACK FOUND')
    else:
        log.warning('Not inside actor-runtime')

    db, undo_sigint = mk_mpdb()
    Lock.local_task_in_debug = 'sync'
    # db.config.enable_hidden_frames = True

    # we entered the global ``breakpoint()`` built-in from sync
    # code?
    frame: FrameType | None = sys._getframe()
    # print(f'FRAME: {str(frame)}')
    # assert not db._is_hidden(frame)

    frame: FrameType = frame.f_back  # type: ignore
    # print(f'FRAME: {str(frame)}')
    # if not db._is_hidden(frame):
    #     pdbp.set_trace()
    # db._hidden_frames.append(
    #     (frame, frame.f_lineno)
    # )
    db.set_trace(frame=frame)
    # NOTE XXX: see the `@pdbp.hideframe` decoration
    # on `Lock.unshield_sigint()`.. I have NO CLUE why
    # the next instruction's def frame is being shown
    # in the tb but it seems to be something wonky with
    # the way `pdb` core works?
    # undo_sigint()

    # Lock.global_actor_in_debug = actor.uid
    # Lock.release()
    # task_can_release_tty_lock.set()


# using the "pause" semantics instead since
# that better covers actually somewhat "pausing the runtime"
# for this particular paralell task to do debugging B)
# pp = pause  # short-hand for "pause point"


async def breakpoint(**kwargs):
    log.warning(
        '`tractor.breakpoint()` is deprecated!\n'
        'Please use `tractor.pause()` instead!\n'
    )
    await pause(**kwargs)


def _post_mortem(
    actor: tractor.Actor,
    pdb: MultiActorPdb,

) -> None:
    '''
    Enter the ``pdbpp`` port mortem entrypoint using our custom
    debugger instance.

    '''
    log.pdb(f"\nAttaching to pdb in crashed actor: {actor.uid}\n")

    # TODO: you need ``pdbpp`` master (at least this commit
    # https://github.com/pdbpp/pdbpp/commit/b757794857f98d53e3ebbe70879663d7d843a6c2)
    # to fix this and avoid the hang it causes. See issue:
    # https://github.com/pdbpp/pdbpp/issues/480
    # TODO: help with a 3.10+ major release if/when it arrives.

    pdbp.xpm(Pdb=lambda: pdb)


post_mortem = partial(
    pause,
    _post_mortem,
)


async def _maybe_enter_pm(err):
    if (
        debug_mode()

        # NOTE: don't enter debug mode recursively after quitting pdb
        # Iow, don't re-enter the repl if the `quit` command was issued
        # by the user.
        and not isinstance(err, bdb.BdbQuit)

        # XXX: if the error is the likely result of runtime-wide
        # cancellation, we don't want to enter the debugger since
        # there's races between when the parent actor has killed all
        # comms and when the child tries to contact said parent to
        # acquire the tty lock.

        # Really we just want to mostly avoid catching KBIs here so there
        # might be a simpler check we can do?
        and not is_multi_cancelled(err)
    ):
        log.debug("Actor crashed, entering debug mode")
        try:
            await post_mortem()
        finally:
            Lock.release()
            return True

    else:
        return False


@acm
async def acquire_debug_lock(
    subactor_uid: tuple[str, str],
) -> AsyncGenerator[None, tuple]:
    '''
    Grab root's debug lock on entry, release on exit.

    This helper is for actor's who don't actually need
    to acquired the debugger but want to wait until the
    lock is free in the process-tree root.

    '''
    if not debug_mode():
        yield None
        return

    async with trio.open_nursery() as n:
        cs = await n.start(
            wait_for_parent_stdin_hijack,
            subactor_uid,
        )
        yield None
        cs.cancel()


async def maybe_wait_for_debugger(
    poll_steps: int = 2,
    poll_delay: float = 0.1,
    child_in_debug: bool = False,

) -> None:

    if (
        not debug_mode()
        and not child_in_debug
    ):
        return

    if (
        is_root_process()
    ):
        # If we error in the root but the debugger is
        # engaged we don't want to prematurely kill (and
        # thus clobber access to) the local tty since it
        # will make the pdb repl unusable.
        # Instead try to wait for pdb to be released before
        # tearing down.
        sub_in_debug: tuple[str, str] | None = None

        for _ in range(poll_steps):

            if Lock.global_actor_in_debug:
                sub_in_debug = tuple(Lock.global_actor_in_debug)

            log.debug('Root polling for debug')

            with trio.CancelScope(shield=True):
                await trio.sleep(poll_delay)

                # TODO: could this make things more deterministic?  wait
                # to see if a sub-actor task will be scheduled and grab
                # the tty lock on the next tick?
                # XXX: doesn't seem to work
                # await trio.testing.wait_all_tasks_blocked(cushion=0)

                debug_complete = Lock.no_remote_has_tty
                if (
                    debug_complete
                    and sub_in_debug is not None
                    and not debug_complete.is_set()
                ):
                    log.pdb(
                        'Root has errored but pdb is in use by '
                        f'child {sub_in_debug}\n'
                        'Waiting on tty lock to release..'
                    )

                    await debug_complete.wait()

                await trio.sleep(poll_delay)
                continue
        else:
            log.debug(
                    'Root acquired TTY LOCK'
            )


# TODO: better naming and what additionals?
# - [ ] optional runtime plugging?
# - [ ] detection for sync vs. async code?
# - [ ] specialized REPL entry when in distributed mode?
# - [x] allow ignoring kbi Bo
@cm
def open_crash_handler(
    catch: set[BaseException] = {
        Exception,
        BaseException,
    },
    ignore: set[BaseException] = {
        KeyboardInterrupt,
    },
):
    '''
    Generic "post mortem" crash handler using `pdbp` REPL debugger.

    We expose this as a CLI framework addon to both `click` and
    `typer` users so they can quickly wrap cmd endpoints which get
    automatically wrapped to use the runtime's `debug_mode: bool`
    AND `pdbp.pm()` around any code that is PRE-runtime entry
    - any sync code which runs BEFORE the main call to
      `trio.run()`.

    '''
    try:
        yield
    except tuple(catch) as err:

        if type(err) not in ignore:
            pdbp.xpm()

        raise


@cm
def maybe_open_crash_handler(pdb: bool = False):
    '''
    Same as `open_crash_handler()` but with bool input flag
    to allow conditional handling.

    Normally this is used with CLI endpoints such that if the --pdb
    flag is passed the pdb REPL is engaed on any crashes B)
    '''
    rtctx = nullcontext
    if pdb:
        rtctx = open_crash_handler

    with rtctx():
        yield
