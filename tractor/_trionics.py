"""
``trio`` inspired apis and helpers
"""
import multiprocessing as mp
from typing import Tuple, List, Dict, Optional, Any
import typing

import trio
from async_generator import asynccontextmanager

from ._state import current_actor
from .log import get_logger, get_loglevel
from ._actor import Actor  # , ActorFailure
from ._portal import Portal
from . import _spawn


log = get_logger('tractor')


class ActorNursery:
    """Spawn scoped subprocess actors.
    """
    def __init__(
        self,
        actor: Actor,
        ria_nursery: trio.Nursery,
        da_nursery: trio.Nursery,
        errors: Dict[str, Exception],
    ) -> None:
        # self.supervisor = supervisor  # TODO
        self._actor: Actor = actor
        self._ria_nursery = ria_nursery
        self._da_nursery = da_nursery
        self._children: Dict[
            Tuple[str, str],
            Tuple[Actor, mp.Process, Optional[Portal]]
        ] = {}
        # portals spawned with ``run_in_actor()`` are
        # cancelled when their "main" result arrives
        self._cancel_after_result_on_exit: set = set()
        self.cancelled: bool = False
        self._join_procs = trio.Event()
        self.errors = errors

    async def start_actor(
        self,
        name: str,
        bind_addr: Tuple[str, int] = ('127.0.0.1', 0),
        statespace: Optional[Dict[str, Any]] = None,
        rpc_module_paths: List[str] = None,
        loglevel: str = None,  # set log level per subactor
        nursery: trio.Nursery = None,
    ) -> Portal:
        loglevel = loglevel or self._actor.loglevel or get_loglevel()

        subactor = Actor(
            name,
            # modules allowed to invoked funcs from
            rpc_module_paths=rpc_module_paths,
            statespace=statespace,  # global proc state vars
            loglevel=loglevel,
            arbiter_addr=current_actor()._arb_addr,
        )
        parent_addr = self._actor.accept_addr
        assert parent_addr

        # start a task to spawn a process
        # blocks until process has been started and a portal setup
        nursery = nursery or self._da_nursery
        return await nursery.start(
            _spawn.new_proc,
            name,
            self,
            subactor,
            self.errors,
            bind_addr,
            parent_addr,
            nursery,
        )

    async def run_in_actor(
        self,
        name: str,
        fn: typing.Callable,
        bind_addr: Tuple[str, int] = ('127.0.0.1', 0),
        rpc_module_paths: Optional[List[str]] = None,
        statespace: Dict[str, Any] = None,
        loglevel: str = None,  # set log level per subactor
        **kwargs,  # explicit args to ``fn``
    ) -> Portal:
        """Spawn a new actor, run a lone task, then terminate the actor and
        return its result.

        Actors spawned using this method are kept alive at nursery teardown
        until the task spawned by executing ``fn`` completes at which point
        the actor is terminated.
        """
        mod_path = fn.__module__
        portal = await self.start_actor(
            name,
            rpc_module_paths=[mod_path] + (rpc_module_paths or []),
            bind_addr=bind_addr,
            statespace=statespace,
            loglevel=loglevel,
            # use the run_in_actor nursery
            nursery=self._ria_nursery,
        )
        # this marks the actor to be cancelled after its portal result
        # is retreived, see logic in `open_nursery()` below.
        self._cancel_after_result_on_exit.add(portal)
        await portal._submit_for_result(
            mod_path,
            fn.__name__,
            **kwargs
        )
        return portal

    async def cancel(self, hard_kill: bool = False) -> None:
        """Cancel this nursery by instructing each subactor to cancel
        itself and wait for all subactors to terminate.

        If ``hard_killl`` is set to ``True`` then kill the processes
        directly without any far end graceful ``trio`` cancellation.
        """
        def do_hard_kill(proc):
            log.warning(f"Hard killing subactors {self._children}")
            proc.terminate()
            # XXX: below doesn't seem to work?
            # send KeyBoardInterrupt (trio abort signal) to sub-actors
            # os.kill(proc.pid, signal.SIGINT)

        log.debug(f"Cancelling nursery")
        with trio.move_on_after(3) as cs:
            async with trio.open_nursery() as nursery:
                for subactor, proc, portal in self._children.values():
                    if hard_kill:
                        do_hard_kill(proc)
                    else:
                        if portal is None:  # actor hasn't fully spawned yet
                            event = self._actor._peer_connected[subactor.uid]
                            log.warning(
                                f"{subactor.uid} wasn't finished spawning?")
                            await event.wait()
                            # channel/portal should now be up
                            _, _, portal = self._children[subactor.uid]

                            # XXX should be impossible to get here
                            # unless method was called from within
                            # shielded cancel scope.
                            if portal is None:
                                # cancelled while waiting on the event
                                # to arrive
                                chan = self._actor._peers[subactor.uid][-1]
                                if chan:
                                    portal = Portal(chan)
                                else:  # there's no other choice left
                                    do_hard_kill(proc)

                        # spawn cancel tasks for each sub-actor
                        assert portal
                        nursery.start_soon(portal.cancel_actor)

        # if we cancelled the cancel (we hung cancelling remote actors)
        # then hard kill all sub-processes
        if cs.cancelled_caught:
            log.error(f"Failed to gracefully cancel {self}, hard killing!")
            async with trio.open_nursery():
                for subactor, proc, portal in self._children.values():
                    nursery.start_soon(do_hard_kill, proc)

        # mark ourselves as having (tried to have) cancelled all subactors
        self.cancelled = True
        self._join_procs.set()


@asynccontextmanager
async def open_nursery() -> typing.AsyncGenerator[ActorNursery, None]:
    """Create and yield a new ``ActorNursery``.
    """
    # TODO: figure out supervisors from erlang

    actor = current_actor()
    if not actor:
        raise RuntimeError("No actor instance has been defined yet?")

    # XXX we use these nurseries because TRIP is doing all its stuff with
    # an `@asynccontextmanager` which has an internal nursery *and* the
    # task that opens a nursery must also close it.
    errors: Dict[str, Exception] = {}
    async with trio.open_nursery() as da_nursery:
        try:
            async with trio.open_nursery() as ria_nursery:
                anursery = ActorNursery(
                    actor, ria_nursery, da_nursery, errors
                )
                try:
                    # spawning of actors happens in this scope after
                    # we yield to the caller.
                    yield anursery
                    log.debug(
                        f"Waiting on subactors {anursery._children}"
                        "to complete"
                    )
                except (BaseException, Exception) as err:
                    anursery._join_procs.set()
                    try:
                        # XXX: hypothetically an error could be raised and then
                        # a cancel signal shows up slightly after in which case
                        # the `else:` block here might not complete?
                        # For now, shield both.
                        with trio.CancelScope(shield=True):
                            if err in (trio.Cancelled, KeyboardInterrupt):
                                log.warning(
                                    f"Nursery for {current_actor().uid} was "
                                    f"cancelled with {err}")
                            else:
                                log.exception(
                                    f"Nursery for {current_actor().uid} "
                                    f"errored with {err}, ")
                            await anursery.cancel()
                    except trio.MultiError as merr:
                        if err not in merr.exceptions:
                            raise trio.MultiError(merr.exceptions + [err])
                    else:
                        raise

                # last bit before first nursery block ends
                log.debug(f"Waiting on all subactors to complete")
                anursery._join_procs.set()
            # ria_nursery scope
        except (Exception, trio.MultiError) as err:
            log.warning(f"Nursery cancelling due to {err}")
            if anursery._children:
                with trio.CancelScope(shield=True):
                    await anursery.cancel()
            raise
        finally:
            if errors:
                if anursery._children:
                    with trio.CancelScope(shield=True):
                        await anursery.cancel()
                if len(errors) > 1:
                    raise trio.MultiError(errors.values())
                else:
                    raise list(errors.values())[0]
    log.debug(f"Nursery teardown complete")
