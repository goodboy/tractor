"""
Multi-core debugging for da peeps!
"""
import sys
from functools import partial
from typing import Awaitable, Tuple, Optional, Callable

from async_generator import aclosing
import tractor
import trio

from .log import get_logger

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


# placeholder for function to set a ``trio.Event``
_pdb_release_hook: Optional[Callable] = None


class TractorConfig(pdbpp.DefaultConfig):
    """Custom ``pdbpp`` goodness.
    """
    sticky_by_default = True

    def teardown(self):
        _pdb_release_hook()


class PdbwTeardown(pdbpp.Pdb):
    """Add teardown hooks to the regular ``pdbpp.Pdb``.
    """
    # override the pdbpp config with our coolio one
    DefaultConfig = TractorConfig

    # TODO: figure out how to dissallow recursive .set_trace() entry
    # since that'll cause deadlock for us.
    def set_continue(self):
        super().set_continue()
        self.config.teardown()

    def set_quit(self):
        super().set_quit()
        self.config.teardown()


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


async def _hijack_stdin_relay_to_child(
    subactor_uid: Tuple[str, str]
) -> None:
    actor = tractor.current_actor()
    debug_lock = actor.statespace.setdefault(
        '_debug_lock', trio.StrictFIFOLock()
    )

    log.debug(f"Actor {subactor_uid} is waiting on stdin hijack lock")
    await debug_lock.acquire()
    log.warning(f"Actor {subactor_uid} acquired stdin hijack lock")

    # TODO: when we get to true remote debugging
    # this will deliver stdin data
    try:
        # indicate to child that we've locked stdio
        yield 'Locked'

        # wait for cancellation of stream by child
        await trio.sleep_forever()

        # TODO: for remote debugging schedule hijacking in root scope
        # (see above)
        # actor._root_nursery.start_soon(hijack_stdin)

    finally:
        if debug_lock.locked():
            debug_lock.release()
            log.debug(f"Actor {subactor_uid} released stdin hijack lock")


# XXX: We only make this sync in case someone wants to
# overload the ``breakpoint()`` built-in.
def _breakpoint(debug_func) -> Awaitable[None]:
    """``tractor`` breakpoint entry for engaging pdb machinery
    in subactors.
    """
    actor = tractor.current_actor()
    do_unlock = trio.Event()

    async def wait_for_parent_stdin_hijack(
        task_status=trio.TASK_STATUS_IGNORED
    ):

        # TODO: need a more robust check for the "root" actor
        if actor._parent_chan:
            try:
                async with tractor._portal.open_portal(
                    actor._parent_chan,
                    start_msg_loop=False,
                    shield=True,
                ) as portal:
                    # with trio.fail_after(1):
                    agen = await portal.run(
                        'tractor._debug',
                        '_hijack_stdin_relay_to_child',
                        subactor_uid=actor.uid,
                    )
                    async with aclosing(agen):
                        async for val in agen:
                            assert val == 'Locked'
                            task_status.started()
                            with trio.CancelScope(shield=True):
                                await do_unlock.wait()

                                # trigger cancellation of remote stream
                                break
            finally:
                log.debug(f"Exiting debugger for actor {actor}")
                actor.statespace['_in_debug'] = False
                log.debug(f"Child {actor} released parent stdio lock")

    async def _bp():
        """Async breakpoint which schedules a parent stdio lock, and once complete
        enters the ``pdbpp`` debugging console.
        """
        in_debug = actor.statespace.setdefault('_in_debug', False)

        if in_debug:
            log.warning(f"Actor {actor} already has a debug lock, skipping...")
            return

        # assign unlock callback for debugger teardown hooks
        global _pdb_release_hook
        _pdb_release_hook = do_unlock.set

        actor.statespace['_in_debug'] = True

        # this **must** be awaited by the caller and is done using the
        # root nursery so that the debugger can continue to run without
        # being restricted by the scope of a new task nursery.
        await actor._service_n.start(wait_for_parent_stdin_hijack)

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
    pdbpp.xpm(Pdb=PdbwTeardown)


post_mortem = partial(
    _breakpoint,
    _post_mortem,
)
