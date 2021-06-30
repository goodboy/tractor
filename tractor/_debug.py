"""
Multi-core debugging for da peeps!

"""
import bdb
import sys
from functools import partial
from contextlib import asynccontextmanager
from typing import Tuple, Optional, Callable, AsyncIterator

import tractor
import trio

from .log import get_logger
from . import _state
from ._discovery import get_root
from ._state import is_root_process
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


class PdbwTeardown(pdbpp.Pdb):
    """Add teardown hooks to the regular ``pdbpp.Pdb``.
    """
    # override the pdbpp config with our coolio one
    DefaultConfig = TractorConfig

    # TODO: figure out how to dissallow recursive .set_trace() entry
    # since that'll cause deadlock for us.
    def set_continue(self):
        try:
            super().set_continue()
        finally:
            global _local_task_in_debug
            _local_task_in_debug = None
            _pdb_release_hook()

    def set_quit(self):
        try:
            super().set_quit()
        finally:
            global _local_task_in_debug
            _local_task_in_debug = None
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


@asynccontextmanager
async def _acquire_debug_lock(uid: Tuple[str, str]) -> AsyncIterator[None]:
    '''Acquire a actor local FIFO lock meant to mutex entry to a local
    debugger entry point to avoid tty clobbering a global root process.

    '''
    global _debug_lock, _global_actor_in_debug, _no_remote_has_tty

    task_name = trio.lowlevel.current_task().name

    log.pdb(
        f"Attempting to acquire TTY lock, remote task: {task_name}:{uid}"
    )

    we_acquired = False

    if _no_remote_has_tty is None:
        # mark the tty lock as being in use so that the runtime
        # can try to avoid clobbering any connection from a child
        # that's currently relying on it.
        _no_remote_has_tty = trio.Event()

    try:
        log.debug(
            f"entering lock checkpoint, remote task: {task_name}:{uid}"
        )
        we_acquired = True
        await _debug_lock.acquire()

        # we_acquired = True

        _global_actor_in_debug = uid
        log.debug(f"TTY lock acquired, remote task: {task_name}:{uid}")

        # NOTE: critical section!
        # this yield is unshielded.
        # IF we received a cancel during the shielded lock
        # entry of some next-in-queue requesting task,
        # then the resumption here will result in that
        # Cancelled being raised to our caller below!

        # in this case the finally below should trigger
        # and the surrounding calle side context should cancel
        # normally relaying back to the caller.

        yield _debug_lock

    finally:
        # if _global_actor_in_debug == uid:
        if we_acquired and _debug_lock.locked():
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
            log.pdb(f"No more tasks waiting on tty lock! says {uid}")
            _no_remote_has_tty.set()
            _no_remote_has_tty = None

        _global_actor_in_debug = None

        log.debug(f"TTY lock released, remote task: {task_name}:{uid}")


# @contextmanager
# def _disable_sigint():
#     try:
#         # disable sigint handling while in debug
#         prior_handler = signal.signal(signal.SIGINT, handler)
#         yield
#     finally:
#         # restore SIGINT handling
#         signal.signal(signal.SIGINT, prior_handler)


@tractor.context
async def _hijack_stdin_relay_to_child(

    ctx: tractor.Context,
    subactor_uid: Tuple[str, str]

) -> str:
    '''Hijack the tty in the root process of an actor tree such that
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

    with trio.CancelScope(shield=True):

        async with _acquire_debug_lock(subactor_uid):

            # indicate to child that we've locked stdio
            await ctx.started('Locked')
            log.pdb(  # type: ignore
                f"Actor {subactor_uid} ACQUIRED stdin hijack lock")

            # wait for unlock pdb by child
            async with ctx.open_stream() as stream:
                try:
                    assert await stream.receive() == 'pdb_unlock'

                except trio.BrokenResourceError:
                    # XXX: there may be a race with the portal teardown
                    # with the calling actor which we can safely ignore
                    # the alternative would be sending an ack message
                    # and allowing the client to wait for us to teardown
                    # first?
                    pass

    log.debug(
        f"TTY lock released, remote task: {task_name}:{subactor_uid}")

    return "pdb_unlock_complete"


async def _breakpoint(

    debug_func,

    # TODO:
    # shield: bool = False

) -> None:
    '''``tractor`` breakpoint entry for engaging pdb machinery
    in the root or a subactor.

    '''
    # TODO: is it possible to debug a trio.Cancelled except block?
    # right now it seems like we can kinda do with by shielding
    # around ``tractor.breakpoint()`` but not if we move the shielded
    # scope here???
    # with trio.CancelScope(shield=shield):

    actor = tractor.current_actor()
    task_name = trio.lowlevel.current_task().name

    global _local_pdb_complete, _pdb_release_hook
    global _local_task_in_debug, _global_actor_in_debug

    await trio.lowlevel.checkpoint()

    async def wait_for_parent_stdin_hijack(
        task_status=trio.TASK_STATUS_IGNORED
    ):
        global _debugger_request_cs

        with trio.CancelScope(shield=True) as cs:
            _debugger_request_cs = cs

            try:
                async with get_root() as portal:

                    log.error('got portal')

                    # this syncs to child's ``Context.started()`` call.
                    async with portal.open_context(

                        tractor._debug._hijack_stdin_relay_to_child,
                        subactor_uid=actor.uid,

                    ) as (ctx, val):

                        log.error('locked context')
                        assert val == 'Locked'

                        async with ctx.open_stream() as stream:

                            log.error('opened stream')
                            # unblock local caller
                            task_status.started()

                            try:
                                await _local_pdb_complete.wait()

                            finally:
                                # TODO: shielding currently can cause hangs...
                                with trio.CancelScope(shield=True):
                                    await stream.send('pdb_unlock')

                            # sync with callee termination
                            assert await ctx.result() == "pdb_unlock_complete"

            except tractor.ContextCancelled:
                log.warning('Root actor cancelled debug lock')

            finally:
                log.debug(f"Exiting debugger for actor {actor}")
                global _local_task_in_debug
                _local_task_in_debug = None
                log.debug(f"Child {actor} released parent stdio lock")

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

        # assign unlock callback for debugger teardown hooks
        _pdb_release_hook = _local_pdb_complete.set

        # this **must** be awaited by the caller and is done using the
        # root nursery so that the debugger can continue to run without
        # being restricted by the scope of a new task nursery.

        # NOTE: if we want to debug a trio.Cancelled triggered exception
        # we have to figure out how to avoid having the service nursery
        # cancel on this task start? I *think* this works below?
        # actor._service_n.cancel_scope.shield = shield
        with trio.CancelScope(shield=True):
            await actor._service_n.start(wait_for_parent_stdin_hijack)

    elif is_root_process():

        # we also wait in the root-parent for any child that
        # may have the tty locked prior
        global _debug_lock

        # TODO: wait, what about multiple root tasks acquiring
        # it though.. shrug?
        # root process (us) already has it; ignore
        if _global_actor_in_debug == actor.uid:
            return

        # XXX: since we need to enter pdb synchronously below,
        # we have to release the lock manually from pdb completion
        # callbacks. Can't think of a nicer way then this atm.
        if _debug_lock.locked():
            log.warning(
                'Root actor attempting to acquire active tty lock'
                f' owned by {_global_actor_in_debug}')

        await _debug_lock.acquire()

        _global_actor_in_debug = actor.uid
        _local_task_in_debug = task_name

        # the lock must be released on pdb completion
        def teardown():
            global _local_pdb_complete, _debug_lock
            global _global_actor_in_debug, _local_task_in_debug

            _debug_lock.release()
            _global_actor_in_debug = None
            _local_task_in_debug = None
            _local_pdb_complete.set()

        _pdb_release_hook = teardown

    # block here one (at the appropriate frame *up* where
    # ``breakpoint()`` was awaited and begin handling stdio
    log.debug("Entering the synchronous world of pdb")
    debug_func(actor)


def _mk_pdb():
    # XXX: setting these flags on the pdb instance are absolutely
    # critical to having ctrl-c work in the ``trio`` standard way!
    # The stdlib's pdb supports entering the current sync frame
    # on a SIGINT, with ``trio`` we pretty much never want this
    # and we did we can handle it in the ``tractor`` task runtime.

    pdb = PdbwTeardown()
    pdb.allow_kbdint = True
    pdb.nosigint = True

    return pdb


def _set_trace(actor=None):
    pdb = _mk_pdb()

    if actor is not None:
        log.pdb(f"\nAttaching pdb to actor: {actor.uid}\n")  # type: ignore

        pdb.set_trace(
            # start 2 levels up in user code
            frame=sys._getframe().f_back.f_back,
        )

    else:
        # we entered the global ``breakpoint()`` built-in from sync code
        global _local_task_in_debug, _pdb_release_hook
        _local_task_in_debug = 'sync'

        def nuttin():
            pass

        _pdb_release_hook = nuttin

        pdb.set_trace(
            # start 2 levels up in user code
            frame=sys._getframe().f_back,
        )


breakpoint = partial(
    _breakpoint,
    _set_trace,
)


def _post_mortem(actor):
    log.pdb(f"\nAttaching to pdb in crashed actor: {actor.uid}\n")
    pdb = _mk_pdb()

    # custom Pdb post-mortem entry
    pdbpp.xpm(Pdb=lambda: pdb)


post_mortem = partial(
    _breakpoint,
    _post_mortem,
)


async def _maybe_enter_pm(err):
    if (
        _state.debug_mode()

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
