"""
Multi-core debugging for da peeps!
"""
import bdb
import sys
from functools import partial
from contextlib import asynccontextmanager, contextmanager
from typing import Awaitable, Tuple, Optional, Callable
import signal

from async_generator import aclosing
import tractor
import trio

from .log import get_logger
from . import _state

try:
    # wtf: only exported when installed in dev mode?
    import pdbpp
except ImportError:
    # pdbpp is installed in regular mode...
    import pdb
    assert pdb.xpm, "pdbpp is not installed?"
    pdbpp = pdb

log = get_logger(__name__)


__all__ = ['breakpoint', 'post_mortem']


# placeholder for function to set a ``trio.Event`` on debugger exit
_pdb_release_hook: Optional[Callable] = None

# actor-wide flag
_in_debug = False

# lock in root actor preventing multi-access to local tty
_debug_lock = trio.StrictFIFOLock()


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
        global _in_debug
        try:
            super().set_continue()
        finally:
            _in_debug = False
            _pdb_release_hook()

    def set_quit(self):
        global _in_debug
        try:
            super().set_quit()
        finally:
            _in_debug = False
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
#             log.trace(f"Stdin input:\n{msg}")
#             # encode to bytes
#             bmsg = str.encode(msg)

#             # relay bytes to subproc over pipe
#             # await proc.stdin.send_all(bmsg)

#             if bmsg in _pdb_exit_patterns:
#                 log.info("Closing stdin hijack")
#                 break


@asynccontextmanager
async def _acquire_debug_lock():
    """Acquire a actor local FIFO lock meant to mutex entry to a local
    debugger entry point to avoid tty clobbering by multiple processes.
    """
    try:
        log.error("TTY BEING ACQUIRED")
        await _debug_lock.acquire()
        log.error("TTY lock acquired")
        yield
    finally:
        if _debug_lock.locked():
            _debug_lock.release()
        log.error("TTY lock released")


def handler(signum, frame):
    """Block SIGINT while in debug to avoid deadlocks with cancellation.
    """
    print(
        "tractor ignores SIGINT while in debug mode\n"
        "If you have a special need for it please open an issue.\n"
    )


# don't allow those stdlib mofos to mess with sigint handler
pdbpp.pdb.Pdb.sigint_handler = handler


@contextmanager
def _disable_sigint():
    try:
        # disable sigint handling while in debug
        prior_handler = signal.signal(signal.SIGINT, handler)
        yield
    finally:
        # restore SIGINT handling
        signal.signal(signal.SIGINT, prior_handler)


async def _hijack_stdin_relay_to_child(
    subactor_uid: Tuple[str, str]
) -> None:
    actor = tractor.current_actor()
    nursery = actor._actoruid2nursery[subactor_uid]
    print(f'NURSERY: {nursery}')
    print(f'nursery is cancelled {nursery.cancelled}')
    if actor._is_cancelled or nursery.cancelled:
        raise RuntimeError(
            "Can not engage debugger actor is already cancelled")

    await trio.sleep(0)

    # TODO: when we get to true remote debugging
    # this will deliver stdin data
    log.warning(f"Actor {subactor_uid} is WAITING on stdin hijack lock")
    async with _acquire_debug_lock():
        log.warning(f"Actor {subactor_uid} ACQUIRED stdin hijack lock")

        with _disable_sigint():
            # indicate to child that we've locked stdio
            yield 'Locked'

            # wait for cancellation of stream by child
            await trio.sleep_forever()

    log.debug(f"Actor {subactor_uid} RELEASED stdin hijack lock")


# XXX: We only make this sync in case someone wants to
# overload the ``breakpoint()`` built-in.
def _breakpoint(debug_func) -> Awaitable[None]:
    """``tractor`` breakpoint entry for engaging pdb machinery
    in subactors.
    """
    global _in_debug
    actor = tractor.current_actor()
    do_unlock = trio.Event()

    async def wait_for_parent_stdin_hijack(
        task_status=trio.TASK_STATUS_IGNORED
    ):
        try:
            async with tractor._portal.open_portal(
                actor._parent_chan,
                start_msg_loop=False,
                # shield=True,
            ) as portal:
                with trio.fail_after(1):
                    agen = await portal.run(
                        'tractor._debug',
                        '_hijack_stdin_relay_to_child',
                        subactor_uid=actor.uid,
                    )
                async with aclosing(agen):

                    # block until first yield above
                    async for val in agen:

                        assert val == 'Locked'
                        task_status.started()

                        # with trio.CancelScope(shield=True):
                        await do_unlock.wait()

                        # trigger cancellation of remote stream
                        break
        finally:
            log.debug(f"Exiting debugger for actor {actor}")
            global _in_debug
            _in_debug = False
            # actor.statespace['_in_debug'] = False
            log.debug(f"Child {actor} released parent stdio lock")

    async def _bp():
        """Async breakpoint which schedules a parent stdio lock, and once complete
        enters the ``pdbpp`` debugging console.
        """
        global _in_debug
        # in_debug = actor.statespace.setdefault('_in_debug', False)

        if _in_debug:
            # if **this** actor is already in debug mode block here
            # waiting for the control to be released - this allows
            # support for recursive entries to `tractor.breakpoint()`
            log.warning(
                f"Actor {actor.uid} already has a debug lock, waiting...")
            await do_unlock.wait()
            await trio.sleep(0.1)
            # return

        # assign unlock callback for debugger teardown hooks
        global _pdb_release_hook
        _pdb_release_hook = do_unlock.set

        # actor.statespace['_in_debug'] = True
        _in_debug = True

        # TODO: need a more robust check for the "root" actor
        if actor._parent_chan:
            # this **must** be awaited by the caller and is done using the
            # root nursery so that the debugger can continue to run without
            # being restricted by the scope of a new task nursery.
            await actor._service_n.start(wait_for_parent_stdin_hijack)

            # block here one (at the appropriate frame *up* where
            # ``breakpoint()`` was awaited and begin handling stdio
            # debug_func(actor)
        else:
            # we also wait in the root-parent for any child that
            # may have the tty locked prior
            async def _lock(
                task_status=trio.TASK_STATUS_IGNORED
            ):
                async with _acquire_debug_lock():
                    task_status.started()
                    await do_unlock.wait()

            await actor._service_n.start(_lock)

        # block here one (at the appropriate frame *up* where
        # ``breakpoint()`` was awaited and begin handling stdio
        debug_func(actor)

    # user code **must** await this!
    return _bp()


def _set_trace(actor):
    log.critical(f"\nAttaching pdb to actor: {actor.uid}\n")
    PdbwTeardown().set_trace(
        # start 2 levels up in user code
        frame=sys._getframe().f_back.f_back,
    )


breakpoint = partial(
    _breakpoint,
    _set_trace,
)


def _post_mortem(actor):
    log.error(f"\nAttaching to pdb in crashed actor: {actor.uid}\n")
    # custom Pdb post-mortem entry
    pdbpp.xpm(Pdb=PdbwTeardown)


post_mortem = partial(
    _breakpoint,
    _post_mortem,
)


async def _maybe_enter_pm(err):
    if (
        _state.debug_mode()
        and not isinstance(err, bdb.BdbQuit)

        # XXX: if the error is the likely result of runtime-wide
        # cancellation, we don't want to enter the debugger since
        # there's races between when the parent actor has killed all
        # comms and when the child tries to contact said parent to
        # acquire the tty lock.

        # Really we just want to mostly avoid catching KBIs here so there
        # might be a simpler check we can do?
        and trio.MultiError.filter(
            lambda exc: exc if not isinstance(exc, trio.Cancelled) else None,
            err,
        )
    ):
        log.warning("Actor crashed, entering debug mode")
        await post_mortem()
