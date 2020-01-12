"""
``trio`` inspired apis and helpers
"""
import inspect
import importlib
import platform
import multiprocessing as mp
from typing import Tuple, List, Dict, Optional, Any
import typing

import trio
from async_generator import asynccontextmanager, aclosing
import trio_run_in_process

from ._state import current_actor
from .log import get_logger, get_loglevel
from ._actor import Actor, ActorFailure
from ._portal import Portal
from . import _spawn


if platform.system() == 'Windows':
    async def proc_waiter(proc: mp.Process) -> None:
        await trio.hazmat.WaitForSingleObject(proc.sentinel)
else:
    async def proc_waiter(proc: mp.Process) -> None:
        await trio.hazmat.wait_readable(proc.sentinel)


log = get_logger('tractor')


class ActorNursery:
    """Spawn scoped subprocess actors.
    """
    def __init__(self, actor: Actor, nursery: trio.Nursery) -> None:
        # self.supervisor = supervisor  # TODO
        self._actor: Actor = actor
        self._nursery = nursery
        self._children: Dict[
            Tuple[str, str],
            Tuple[Actor, mp.Process, Optional[Portal]]
        ] = {}
        # portals spawned with ``run_in_actor()`` are
        # cancelled when their "main" result arrives
        self._cancel_after_result_on_exit: set = set()
        self.cancelled: bool = False
        # self._aexitstack = contextlib.AsyncExitStack()

    async def __aenter__(self):
        return self

    async def start_actor(
        self,
        name: str,
        bind_addr: Tuple[str, int] = ('127.0.0.1', 0),
        statespace: Optional[Dict[str, Any]] = None,
        rpc_module_paths: List[str] = None,
        loglevel: str = None,  # set log level per subactor
    ) -> Portal:
        loglevel = loglevel or self._actor.loglevel or get_loglevel()

        mods = {}
        for path in rpc_module_paths or ():
            mod = importlib.import_module(path)
            mods[path] = mod.__file__

        actor = Actor(
            name,
            # modules allowed to invoked funcs from
            rpc_module_paths=mods,
            statespace=statespace,  # global proc state vars
            loglevel=loglevel,
            arbiter_addr=current_actor()._arb_addr,
        )
        parent_addr = self._actor.accept_addr
        assert parent_addr
        proc = await _spawn.new_proc(
            name,
            actor,
            bind_addr,
            parent_addr,
            self._nursery,
        )
        # `multiprocessing` only (since no async interface):
        # register the process before start in case we get a cancel
        # request before the actor has fully spawned - then we can wait
        # for it to fully come up before sending a cancel request
        self._children[actor.uid] = (actor, proc, None)

        if not isinstance(proc, trio_run_in_process.process.Process):
            proc.start()
            if not proc.is_alive():
                raise ActorFailure("Couldn't start sub-actor?")

        log.info(f"Started {proc}")
        # wait for actor to spawn and connect back to us
        # channel should have handshake completed by the
        # local actor by the time we get a ref to it
        event, chan = await self._actor.wait_for_peer(actor.uid)
        portal = Portal(chan)
        self._children[actor.uid] = (actor, proc, portal)

        return portal

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
        )
        # this marks the actor to be cancelled after its portal result
        # is retreived, see ``wait()`` below.
        self._cancel_after_result_on_exit.add(portal)
        await portal._submit_for_result(
            mod_path,
            fn.__name__,
            **kwargs
        )
        return portal

    async def wait(self) -> None:
        """Wait for all subactors to complete.

        This is probably the most complicated (and confusing, sorry)
        function that does all the clever crap to deal with cancellation,
        error propagation, and graceful subprocess tear down.
        """
        async def exhaust_portal(portal, actor):
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
                    errors.append(result)
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

        async def wait_for_proc(
            proc: mp.Process,
            actor: Actor,
            portal: Portal,
            cancel_scope: Optional[trio._core._run.CancelScope] = None,
        ) -> None:
            # please god don't hang
            if not isinstance(proc, trio_run_in_process.process.Process):
                # TODO: timeout block here?
                if proc.is_alive():
                    await proc_waiter(proc)
                proc.join()
            else:
                # trio_run_in_process blocking wait
                    if errors:
                        multierror = trio.MultiError(errors)
                        # import pdb; pdb.set_trace()
                        # try:
                        #     with trio.CancelScope(shield=True):
                        #         await proc.mng.__aexit__(
                        #             type(multierror),
                        #             multierror,
                        #             multierror.__traceback__,
                        #         )
                        # except BaseException as err:
                        #     import pdb; pdb.set_trace()
                        #     pass
                    # else:
                        await proc.mng.__aexit__(None, None, None)
                # proc.nursery.cancel_scope.cancel()

            log.debug(f"Joined {proc}")
            # indicate we are no longer managing this subactor
            self._children.pop(actor.uid)

            # proc terminated, cancel result waiter that may have
            # been spawned in tandem if not done already
            if cancel_scope: # and not portal._cancelled:
                log.warning(
                    f"Cancelling existing result waiter task for {actor.uid}")
                cancel_scope.cancel()

        log.debug(f"Waiting on all subactors to complete")
        # since we pop each child subactor on termination,
        # iterate a copy
        children = self._children.copy()
        errors: List[Exception] = []
        # wait on run_in_actor() tasks, unblocks when all complete
        async with trio.open_nursery() as nursery:
        # async with self._nursery as nursery:
            for subactor, proc, portal in children.values():
                cs = None
                # portal from ``run_in_actor()``
                if portal in self._cancel_after_result_on_exit:
                    cs = await nursery.start(
                        cancel_on_completion, portal, subactor)
                    # TODO: how do we handle remote host spawned actors?
                    nursery.start_soon(
                        wait_for_proc, proc, subactor, portal, cs)

        if errors:
            multierror = trio.MultiError(errors)
            if not self.cancelled:
                # bubble up error(s) here and expect to be called again
                # once the nursery has been cancelled externally (ex.
                # from within __aexit__() if an error is caught around
                # ``self.wait()`` then, ``self.cancel()`` is called
                # immediately, in the default supervisor strat, after
                # which in turn ``self.wait()`` is called again.)
                raise trio.MultiError(errors)

        # wait on all `start_actor()` subactors to complete
        # if errors were captured above and we have not been cancelled
        # then these ``start_actor()`` spawned actors will block until
        # cancelled externally
        children = self._children.copy()
        async with trio.open_nursery() as nursery:
            for subactor, proc, portal in children.values():
                # TODO: how do we handle remote host spawned actors?
                nursery.start_soon(wait_for_proc, proc, subactor, portal, cs)

        log.debug(f"All subactors for {self} have terminated")
        if errors:
            # always raise any error if we're also cancelled
            raise trio.MultiError(errors)

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
            async with trio.open_nursery() as n:
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
                        n.start_soon(portal.cancel_actor)

        # if we cancelled the cancel (we hung cancelling remote actors)
        # then hard kill all sub-processes
        if cs.cancelled_caught:
            log.error(f"Failed to gracefully cancel {self}, hard killing!")
            async with trio.open_nursery() as n:
                for subactor, proc, portal in self._children.values():
                    n.start_soon(do_hard_kill, proc)

        # mark ourselves as having (tried to have) cancelled all subactors
        self.cancelled = True
        await self.wait()

    async def __aexit__(self, etype, value, tb):
        """Wait on all subactor's main routines to complete.
        """
        # XXX: this is effectively the (for now) lone
        # cancellation/supervisor strategy (one-cancels-all)
        # which exactly mimicks trio's behaviour
        if etype is not None:
            try:
                # XXX: hypothetically an error could be raised and then
                # a cancel signal shows up slightly after in which case
                # the `else:` block here might not complete?
                # For now, shield both.
                with trio.CancelScope(shield=True):
                    if etype in (trio.Cancelled, KeyboardInterrupt):
                        log.warning(
                            f"Nursery for {current_actor().uid} was "
                            f"cancelled with {etype}")
                    else:
                        log.exception(
                            f"Nursery for {current_actor().uid} "
                            f"errored with {etype}, ")
                    await self.cancel()
            except trio.MultiError as merr:
                if value not in merr.exceptions:
                    raise trio.MultiError(merr.exceptions + [value])
                raise
        else:
            log.debug(f"Waiting on subactors {self._children} to complete")
            try:
                await self.wait()
            except (Exception, trio.MultiError) as err:
                log.warning(f"Nursery cancelling due to {err}")
                if self._children:
                    with trio.CancelScope(shield=True):
                        await self.cancel()
                raise

            log.debug(f"Nursery teardown complete")


@asynccontextmanager
async def open_nursery() -> typing.AsyncGenerator[ActorNursery, None]:
    """Create and yield a new ``ActorNursery``.
    """
    # TODO: figure out supervisors from erlang

    actor = current_actor()
    if not actor:
        raise RuntimeError("No actor instance has been defined yet?")

    # XXX we need this nursery because TRIP is doing all its stuff with
    # an `@asynccontextmanager` which has an internal nursery *and* the
    # task that opens a nursery must also close it - so we need a path
    # in TRIP to make this all kinda work as well. Note I'm basically
    # giving up for now - it's probably equivalent amounts of work to
    # make TRIP vs. `multiprocessing` work here.
    async with trio.open_nursery() as nursery:
        async with ActorNursery(actor, nursery) as anursery:
            yield anursery
