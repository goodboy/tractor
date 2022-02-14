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
import sys
import signal
from functools import partial
from contextlib import asynccontextmanager as acm
from typing import (
    Tuple,
    Optional,
    Callable,
    AsyncIterator,
    AsyncGenerator,
)
from types import FrameType

import tractor
import trio
from trio_typing import TaskStatus

from .log import get_logger
from ._discovery import get_root
from ._state import is_root_process, debug_mode
from ._exceptions import is_multi_cancelled


try:
    # wtf: only exported when installed in dev mode?
    import pdbpp
except ImportError:
    # pdbpp is installed in regular mode...it monkey patches stuff
    import pdb
    assert pdb.xpm, "pdbpp is not installed?"  # type: ignore
    pdbpp = pdb

log = get_logger(__name__)


__all__ = ['breakpoint', 'post_mortem']


# TODO: wrap all these in a static global class: ``DebugLock`` maybe?

# placeholder for function to set a ``trio.Event`` on debugger exit
_pdb_release_hook: Optional[Callable] = None

# actor-wide variable pointing to current task name using debugger
_local_task_in_debug: Optional[str] = None

# actor tree-wide actor uid that supposedly has the tty lock
_global_actor_in_debug: Optional[Tuple[str, str]] = None

# lock in root actor preventing multi-access to local tty
_debug_lock: trio.StrictFIFOLock = trio.StrictFIFOLock()
_local_pdb_complete: Optional[trio.Event] = None
_no_remote_has_tty: Optional[trio.Event] = None

# XXX: set by the current task waiting on the root tty lock
# and must be cancelled if this actor is cancelled via message
# otherwise deadlocks with the parent actor may ensure
_debugger_request_cs: Optional[trio.CancelScope] = None


class TractorConfig(pdbpp.DefaultConfig):
    """Custom ``pdbpp`` goodness.
    """
    # sticky_by_default = True
    enable_hidden_frames = False


class MultiActorPdb(pdbpp.Pdb):
    '''
    Add teardown hooks to the regular ``pdbpp.Pdb``.

    '''
    # override the pdbpp config with our coolio one
    DefaultConfig = TractorConfig

    # TODO: figure out how to disallow recursive .set_trace() entry
    # since that'll cause deadlock for us.
    def set_continue(self):
        try:
            super().set_continue()
        finally:
            global _local_task_in_debug, _pdb_release_hook
            _local_task_in_debug = None
            if _pdb_release_hook:
                _pdb_release_hook()

    def set_quit(self):
        try:
            super().set_quit()
        finally:
            global _local_task_in_debug, _pdb_release_hook
            _local_task_in_debug = None
            if _pdb_release_hook:
                _pdb_release_hook()


# TODO: will be needed whenever we get to true remote debugging.
# XXX see https://github.com/goodboy/tractor/issues/130

# # TODO: is there some way to determine this programatically?
# _pdb_exit_patterns = tuple(
#     str.encode(patt + "\n") for patt in (
#         'c', 'cont', 'continue', 'q', 'quit')
# )

# def subactoruid2proc(
#     actor: 'Actor',  # noqa
#     uid: Tuple[str, str]
# ) -> trio.Process:
#     n = actor._actoruid2nursery[uid]
#     _, proc, _ = n._children[uid]
#     return proc

# async def hijack_stdin():
#     log.info(f"Hijacking stdin from {actor.uid}")

#     trap std in and relay to subproc
#     async_stdin = trio.wrap_file(sys.stdin)

#     async with aclosing(async_stdin):
#         async for msg in async_stdin:
#             log.runtime(f"Stdin input:\n{msg}")
#             # encode to bytes
#             bmsg = str.encode(msg)

#             # relay bytes to subproc over pipe
#             # await proc.stdin.send_all(bmsg)

#             if bmsg in _pdb_exit_patterns:
#                 log.info("Closing stdin hijack")
#                 break


@acm
async def _acquire_debug_lock(
    uid: Tuple[str, str]

) -> AsyncIterator[trio.StrictFIFOLock]:
    '''
    Acquire a root-actor local FIFO lock which tracks mutex access of
    the process tree's global debugger breakpoint.

    This lock avoids tty clobbering (by preventing multiple processes
    reading from stdstreams) and ensures multi-actor, sequential access
    to the ``pdb`` repl.

    '''
    global _debug_lock, _global_actor_in_debug, _no_remote_has_tty

    task_name = trio.lowlevel.current_task().name

    log.runtime(
        f"Attempting to acquire TTY lock, remote task: {task_name}:{uid}"
    )

    we_acquired = False

    if _no_remote_has_tty is None:
        # mark the tty lock as being in use so that the runtime
        # can try to avoid clobbering any connection from a child
        # that's currently relying on it.
        _no_remote_has_tty = trio.Event()

    try:
        log.runtime(
            f"entering lock checkpoint, remote task: {task_name}:{uid}"
        )
        we_acquired = True
        await _debug_lock.acquire()

        _global_actor_in_debug = uid
        log.runtime(f"TTY lock acquired, remote task: {task_name}:{uid}")

        # NOTE: critical section: this yield is unshielded!

        # IF we received a cancel during the shielded lock entry of some
        # next-in-queue requesting task, then the resumption here will
        # result in that ``trio.Cancelled`` being raised to our caller
        # (likely from ``_hijack_stdin_for_child()`` below)!  In
        # this case the ``finally:`` below should trigger and the
        # surrounding caller side context should cancel normally
        # relaying back to the caller.

        yield _debug_lock

    finally:
        # if _global_actor_in_debug == uid:
        if (
            we_acquired
            and _debug_lock.locked()
        ):
            _debug_lock.release()

        # IFF there are no more requesting tasks queued up fire, the
        # "tty-unlocked" event thereby alerting any monitors of the lock that
        # we are now back in the "tty unlocked" state. This is basically
        # and edge triggered signal around an empty queue of sub-actor
        # tasks that may have tried to acquire the lock.
        stats = _debug_lock.statistics()
        if (
            not stats.owner
        ):
            log.runtime(f"No more tasks waiting on tty lock! says {uid}")
            _no_remote_has_tty.set()
            _no_remote_has_tty = None

        _global_actor_in_debug = None

        log.runtime(f"TTY lock released, remote task: {task_name}:{uid}")


@tractor.context
async def _hijack_stdin_for_child(

    ctx: tractor.Context,
    subactor_uid: Tuple[str, str]

) -> str:
    '''
    Hijack the tty in the root process of an actor tree such that
    the pdbpp debugger console can be allocated to a sub-actor for repl
    bossing.

    '''
    task_name = trio.lowlevel.current_task().name

    # TODO: when we get to true remote debugging
    # this will deliver stdin data?

    log.debug(
        "Attempting to acquire TTY lock\n"
        f"remote task: {task_name}:{subactor_uid}"
    )

    log.debug(f"Actor {subactor_uid} is WAITING on stdin hijack lock")

    orig_handler = signal.signal(
        signal.SIGINT,
        shield_sigint,
    )
    try:
        with (
            trio.CancelScope(shield=True),
        ):
            try:
                lock = None
                async with _acquire_debug_lock(subactor_uid) as lock:

                    # indicate to child that we've locked stdio
                    await ctx.started('Locked')
                    log.debug(
                        f"Actor {subactor_uid} acquired stdin hijack lock"
                    )

                    # wait for unlock pdb by child
                    async with ctx.open_stream() as stream:
                        assert await stream.receive() == 'pdb_unlock'

            except (
                BaseException,
                # trio.MultiError,
                # Exception,
                # trio.BrokenResourceError,
                # trio.Cancelled,  # by local cancellation
                # trio.ClosedResourceError,  # by self._rx_chan
                # ContextCancelled,
                # ConnectionResetError,
            ):
                # XXX: there may be a race with the portal teardown
                # with the calling actor which we can safely ignore.
                # The alternative would be sending an ack message
                # and allowing the client to wait for us to teardown
                # first?
                if lock and lock.locked():
                    lock.release()

                # if isinstance(err, trio.Cancelled):
                raise

            finally:
                log.runtime(
                    "TTY lock released, remote task:"
                    f"{task_name}:{subactor_uid}"
                )

        return "pdb_unlock_complete"

    finally:
        signal.signal(
            signal.SIGINT,
            orig_handler
        )


async def wait_for_parent_stdin_hijack(
    actor_uid: Tuple[str, str],
    task_status: TaskStatus[trio.CancelScope] = trio.TASK_STATUS_IGNORED
):
    '''
    Connect to the root actor via a ctx and invoke a task which locks
    a root-local TTY lock.

    This function is used by any sub-actor to acquire mutex access to
    pdb and the root's TTY for interactive debugging (see below inside
    ``_breakpoint()``). It can be used to ensure that an intermediate
    nursery-owning actor does not clobber its children if they are in
    debug (see below inside ``maybe_wait_for_debugger()``).

    '''
    global _debugger_request_cs

    with trio.CancelScope(shield=True) as cs:
        _debugger_request_cs = cs

        try:
            async with get_root() as portal:

                # this syncs to child's ``Context.started()`` call.
                async with portal.open_context(

                    tractor._debug._hijack_stdin_for_child,
                    subactor_uid=actor_uid,

                ) as (ctx, val):

                    log.pdb('locked context')
                    assert val == 'Locked'

                    async with ctx.open_stream() as stream:
                        # unblock local caller

                        try:
                            assert _local_pdb_complete
                            task_status.started(cs)
                            await _local_pdb_complete.wait()

                        finally:
                            # TODO: shielding currently can cause hangs...
                            # with trio.CancelScope(shield=True):
                            await stream.send('pdb_unlock')

                        # sync with callee termination
                        assert await ctx.result() == "pdb_unlock_complete"

                log.pdb('unlocked context')

        except tractor.ContextCancelled:
            log.warning('Root actor cancelled debug lock')

        finally:
            log.debug(f"Exiting debugger for actor {actor_uid}")
            global _local_task_in_debug
            _local_task_in_debug = None
            log.debug(f"Child {actor_uid} released parent stdio lock")


def mk_mpdb() -> tuple[MultiActorPdb, Callable]:

    pdb = MultiActorPdb()
    signal.signal = pdbpp.hideframe(signal.signal)
    orig_handler = signal.signal(
        signal.SIGINT,
        partial(shield_sigint, pdb_obj=pdb),
    )
    pdb.allow_kbdint = True
    pdb.nosigint = True

    # TODO: add this as method on our pdb obj?
    def undo_sigint():
        # restore original sigint handler
        signal.signal(
            signal.SIGINT,
            orig_handler
        )

    return pdb, undo_sigint


async def _breakpoint(

    debug_func,

    # TODO:
    # shield: bool = False

) -> None:
    '''
    breakpoint entry for engaging pdb machinery in the root or
    a subactor.

    '''
    __tracebackhide__ = True

    pdb, undo_sigint = mk_mpdb()
    actor = tractor.current_actor()
    task_name = trio.lowlevel.current_task().name

    global _local_pdb_complete, _pdb_release_hook
    global _local_task_in_debug, _global_actor_in_debug

    # TODO: is it possible to debug a trio.Cancelled except block?
    # right now it seems like we can kinda do with by shielding
    # around ``tractor.breakpoint()`` but not if we move the shielded
    # scope here???
    # with trio.CancelScope(shield=shield):
    #     await trio.lowlevel.checkpoint()

    if not _local_pdb_complete or _local_pdb_complete.is_set():
        _local_pdb_complete = trio.Event()

    # TODO: need a more robust check for the "root" actor
    if actor._parent_chan and not is_root_process():

        if _local_task_in_debug:
            if _local_task_in_debug == task_name:
                # this task already has the lock and is
                # likely recurrently entering a breakpoint
                return

            # if **this** actor is already in debug mode block here
            # waiting for the control to be released - this allows
            # support for recursive entries to `tractor.breakpoint()`
            log.warning(f"{actor.uid} already has a debug lock, waiting...")

            await _local_pdb_complete.wait()
            await trio.sleep(0.1)

        # mark local actor as "in debug mode" to avoid recurrent
        # entries/requests to the root process
        _local_task_in_debug = task_name

        def child_release_hook():
            # _local_task_in_debug = None
            _local_pdb_complete.set()

            # restore original sigint handler
            undo_sigint()

        # assign unlock callback for debugger teardown hooks
        # _pdb_release_hook = _local_pdb_complete.set
        _pdb_release_hook = child_release_hook

        # this **must** be awaited by the caller and is done using the
        # root nursery so that the debugger can continue to run without
        # being restricted by the scope of a new task nursery.

        # NOTE: if we want to debug a trio.Cancelled triggered exception
        # we have to figure out how to avoid having the service nursery
        # cancel on this task start? I *think* this works below?
        # actor._service_n.cancel_scope.shield = shield
        with trio.CancelScope(shield=True):
            await actor._service_n.start(
                wait_for_parent_stdin_hijack,
                actor.uid,
            )

    elif is_root_process():

        # we also wait in the root-parent for any child that
        # may have the tty locked prior
        global _debug_lock

        # TODO: wait, what about multiple root tasks acquiring it though?
        # root process (us) already has it; ignore
        if _global_actor_in_debug == actor.uid:
            return

        # XXX: since we need to enter pdb synchronously below,
        # we have to release the lock manually from pdb completion
        # callbacks. Can't think of a nicer way then this atm.
        if _debug_lock.locked():
            log.warning(
                'Root actor attempting to shield-acquire active tty lock'
                f' owned by {_global_actor_in_debug}')

            # must shield here to avoid hitting a ``Cancelled`` and
            # a child getting stuck bc we clobbered the tty
            with trio.CancelScope(shield=True):
                await _debug_lock.acquire()
        else:
            # may be cancelled
            await _debug_lock.acquire()

        _global_actor_in_debug = actor.uid
        _local_task_in_debug = task_name

        # the lock must be released on pdb completion
        def teardown():
            global _local_pdb_complete, _debug_lock
            global _global_actor_in_debug, _local_task_in_debug

            try:
                _debug_lock.release()
            except RuntimeError:
                # uhhh makes no sense but been seeing the non-owner
                # release error even though this is definitely the task
                # that locked?
                owner = _debug_lock.statistics().owner
                if owner:
                    raise

            _global_actor_in_debug = None
            _local_task_in_debug = None
            _local_pdb_complete.set()

            # restore original sigint handler
            undo_sigint()

        _pdb_release_hook = teardown

    # frame = sys._getframe()
    # last_f = frame.f_back
    # last_f.f_globals['__tracebackhide__'] = True

    try:
        # block here one (at the appropriate frame *up*) where
        # ``breakpoint()`` was awaited and begin handling stdio.
        log.debug("Entering the synchronous world of pdb")
        debug_func(actor, pdb)

    except bdb.BdbQuit:
        if _pdb_release_hook:
            _pdb_release_hook()
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
    #     # signal.signal = pdbpp.hideframe(signal.signal)
    #     signal.signal(
    #         signal.SIGINT,
    #         orig_handler
    #     )


def shield_sigint(
    signum: int,
    frame: 'frame',  # type: ignore # noqa
    pdb_obj: Optional[MultiActorPdb] = None,
    *args,

) -> None:
    '''
    Specialized debugger compatible SIGINT handler.

    In childred we always ignore to avoid deadlocks since cancellation
    should always be managed by the parent supervising actor. The root
    is always cancelled on ctrl-c.

    '''
    __tracebackhide__ = True

    global _local_task_in_debug, _global_actor_in_debug
    in_debug = _global_actor_in_debug

    actor = tractor.current_actor()

    # root actor branch that reports whether or not a child
    # has locked debugger.
    if (
        is_root_process()
        and in_debug
    ):
        name = in_debug[0]
        if name != 'root':
            log.pdb(
                f"Ignoring SIGINT while child in debug mode: `{in_debug}`"
            )

        else:
            log.pdb(
                "Ignoring SIGINT while in debug mode"
            )

    # child actor that has locked the debugger
    elif (
        not is_root_process()
    ):
        task = _local_task_in_debug
        if task:
            log.pdb(
                f"Ignoring SIGINT while task in debug mode: `{task}`"
            )

        # TODO: how to handle the case of an intermediary-child actor
        # that **is not** marked in debug mode?
        # elif debug_mode():

        else:
            log.pdb(
                "Ignoring SIGINT since debug mode is enabled"
            )

    # noone has the debugger so raise KBI
    else:
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

    # maybe redraw/print last REPL output to console
    if pdb_obj:

        # TODO: make this work like sticky mode where if there is output
        # detected as written to the tty we redraw this part underneath
        # and erase the past draw of this same bit above?
        # pdb_obj.sticky = True
        # pdb_obj._print_if_sticky()

        # also see these links for an approach from ``ptk``:
        # https://github.com/goodboy/tractor/issues/130#issuecomment-663752040
        # https://github.com/prompt-toolkit/python-prompt-toolkit/blob/c2c6af8a0308f9e5d7c0e28cb8a02963fe0ce07a/prompt_toolkit/patch_stdout.py

        pdb_obj.do_longlist(None)
        print(pdb_obj.prompt, end='', flush=True)


def _set_trace(
    actor: Optional[tractor._actor.Actor] = None,
    pdb: Optional[MultiActorPdb] = None,
):
    __tracebackhide__ = True
    actor = actor or tractor.current_actor()

    # XXX: on latest ``pdbpp`` i guess we don't need this?
    # frame = sys._getframe()
    # last_f = frame.f_back
    # last_f.f_globals['__tracebackhide__'] = True

    # start 2 levels up in user code
    frame: FrameType = sys._getframe()
    if frame:
        frame = frame.f_back.f_back  # type: ignore

    if pdb and actor is not None:
        log.pdb(f"\nAttaching pdb to actor: {actor.uid}\n")

    else:
        pdb, undo_sigint = mk_mpdb()

        # we entered the global ``breakpoint()`` built-in from sync code?
        global _local_task_in_debug, _pdb_release_hook
        _local_task_in_debug = 'sync'

    pdb.set_trace(frame=frame)


breakpoint = partial(
    _breakpoint,
    _set_trace,
)


def _post_mortem(
    actor: tractor._actor.Actor,
    pdb: MultiActorPdb,

) -> None:
    '''
    Enter the ``pdbpp`` port mortem entrypoint using our custom
    debugger instance.

    '''
    log.pdb(f"\nAttaching to pdb in crashed actor: {actor.uid}\n")

    # XXX: on py3.10 if you don't have latest ``pdbpp`` installed.
    # The exception looks something like:
    # Traceback (most recent call last):
    # File ".../tractor/_debug.py", line 729, in _post_mortem
    #   for _ in range(100):
    # File "../site-packages/pdb.py", line 1227, in xpm
    #   post_mortem(info[2], Pdb)
    # File "../site-packages/pdb.py", line 1175, in post_mortem
    #   p.interaction(None, t)
    # File "../site-packages/pdb.py", line 216, in interaction
    #   ret = self.setup(frame, traceback)
    # File "../site-packages/pdb.py", line 259, in setup
    #   ret = super(Pdb, self).setup(frame, tb)
    # File "/usr/lib/python3.10/pdb.py", line 217, in setup
    #   self.curframe = self.stack[self.curindex][0]
    # IndexError: list index out of range

    # NOTE: you need ``pdbpp`` master (at least this commit
    # https://github.com/pdbpp/pdbpp/commit/b757794857f98d53e3ebbe70879663d7d843a6c2)
    # to fix this and avoid the hang it causes XD.
    # see also: https://github.com/pdbpp/pdbpp/issues/480

    pdbpp.xpm(Pdb=lambda: pdb)


post_mortem = partial(
    _breakpoint,
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
        await post_mortem()
        return True

    else:
        return False


@acm
async def acquire_debug_lock(
    subactor_uid: Tuple[str, str],
) -> AsyncGenerator[None, tuple]:
    '''
    Grab root's debug lock on entry, release on exit.

    This helper is for actor's who don't actually need
    to acquired the debugger but want to wait until the
    lock is free in the tree root.

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

    if not debug_mode() and not child_in_debug:
        return

    if (
        is_root_process()
    ):
        global _no_remote_has_tty, _global_actor_in_debug, _wait_all_tasks_lock

        # If we error in the root but the debugger is
        # engaged we don't want to prematurely kill (and
        # thus clobber access to) the local tty since it
        # will make the pdb repl unusable.
        # Instead try to wait for pdb to be released before
        # tearing down.

        sub_in_debug = None

        for _ in range(poll_steps):

            if _global_actor_in_debug:
                sub_in_debug = tuple(_global_actor_in_debug)

            log.debug(
                'Root polling for debug')

            with trio.CancelScope(shield=True):
                await trio.sleep(poll_delay)

                # TODO: could this make things more deterministic?  wait
                # to see if a sub-actor task will be scheduled and grab
                # the tty lock on the next tick?
                # XXX: doesn't seem to work
                # await trio.testing.wait_all_tasks_blocked(cushion=0)

                debug_complete = _no_remote_has_tty
                if (
                    (debug_complete and
                     not debug_complete.is_set())
                ):
                    log.debug(
                        'Root has errored but pdb is in use by '
                        f'child {sub_in_debug}\n'
                        'Waiting on tty lock to release..')

                    await debug_complete.wait()

                await trio.sleep(poll_delay)
                continue
        else:
            log.debug(
                    'Root acquired TTY LOCK'
            )
