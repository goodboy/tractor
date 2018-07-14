"""
``trio`` inspired apis and helpers
"""
import multiprocessing as mp

import trio
from async_generator import asynccontextmanager

from ._state import current_actor
from .log import get_logger, get_loglevel
from ._actor import Actor, ActorFailure
from ._portal import Portal


ctx = mp.get_context("forkserver")
log = get_logger('tractor')


class ActorNursery:
    """Spawn scoped subprocess actors.
    """
    def __init__(self, actor, supervisor=None):
        self.supervisor = supervisor  # TODO
        self._actor = actor
        # We'll likely want some way to cancel all sub-actors eventually
        # self.cancel_scope = cancel_scope
        self._children = {}
        self.cancelled = False

    async def __aenter__(self):
        return self

    async def start_actor(
        self,
        name: str,
        main=None,
        bind_addr=('127.0.0.1', 0),
        statespace=None,
        rpc_module_paths=None,
        outlive_main=False,  # sub-actors die when their main task completes
        loglevel=None,  # set log level per subactor
    ):
        loglevel = loglevel or self._actor.loglevel or get_loglevel()
        actor = Actor(
            name,
            # modules allowed to invoked funcs from
            rpc_module_paths=rpc_module_paths or [],
            statespace=statespace,  # global proc state vars
            main=main,  # main coroutine to be invoked
            outlive_main=outlive_main,
            loglevel=loglevel,
            arbiter_addr=current_actor()._arb_addr,
        )
        parent_addr = self._actor.accept_addr
        assert parent_addr
        proc = ctx.Process(
            target=actor._fork_main,
            args=(bind_addr, parent_addr),
            # daemon=True,
            name=name,
        )
        proc.start()
        if not proc.is_alive():
            raise ActorFailure("Couldn't start sub-actor?")

        log.info(f"Started {proc}")
        # wait for actor to spawn and connect back to us
        # channel should have handshake completed by the
        # local actor by the time we get a ref to it
        event, chan = await self._actor.wait_for_peer(actor.uid)
        portal = Portal(chan)
        self._children[(name, proc.pid)] = (actor, proc, portal)
        return portal

    async def wait(self):
        """Wait for all subactors to complete.
        """
        async def wait_for_proc(proc, actor, portal):
            # TODO: timeout block here?
            if proc.is_alive():
                await trio.hazmat.wait_readable(proc.sentinel)
            # please god don't hang
            proc.join()
            log.debug(f"Joined {proc}")
            event = self._actor._peers.get(actor.uid)
            if isinstance(event, trio.Event):
                event.set()
                log.warn(
                    f"Cancelled `wait_for_peer()` call since {actor.uid}"
                    f" is already dead!")
            if not portal._result:
                log.debug(f"Faking result for {actor.uid}")
                q = self._actor.get_waitq(actor.uid, 'main')
                q.put_nowait({'return': None, 'cid': 'main'})

        async def wait_for_result(portal):
            if portal.channel.connected():
                log.debug(f"Waiting on final result from {subactor.uid}")
                await portal.result()

        # unblocks when all waiter tasks have completed
        async with trio.open_nursery() as nursery:
            for subactor, proc, portal in self._children.values():
                nursery.start_soon(wait_for_proc, proc, subactor, portal)
                nursery.start_soon(wait_for_result, portal)

    async def cancel(self, hard_kill=False):
        """Cancel this nursery by instructing each subactor to cancel
        iteslf and wait for all subprocesses to terminate.

        If ``hard_killl`` is set to ``True`` then kill the processes
        directly without any far end graceful ``trio`` cancellation.
        """
        log.debug(f"Cancelling nursery")
        for subactor, proc, portal in self._children.values():
            if proc is mp.current_process():
                # XXX: does this even make sense?
                await subactor.cancel()
            else:
                if hard_kill:
                    log.warn(f"Hard killing subactors {self._children}")
                    proc.terminate()
                    # XXX: doesn't seem to work?
                    # send KeyBoardInterrupt (trio abort signal) to sub-actors
                    # os.kill(proc.pid, signal.SIGINT)
                else:
                    await portal.cancel_actor()

        log.debug(f"Waiting on all subactors to complete")
        await self.wait()
        self.cancelled = True
        log.debug(f"All subactors for {self} have terminated")

    async def __aexit__(self, etype, value, tb):
        """Wait on all subactor's main routines to complete.
        """
        if etype is not None:
            # XXX: hypothetically an error could be raised and then
            # a cancel signal shows up slightly after in which case the
            # else block here might not complete? Should both be shielded?
            if etype is trio.Cancelled:
                with trio.open_cancel_scope(shield=True):
                    log.warn(
                        f"{current_actor().uid} was cancelled with {etype}"
                        ", cancelling actor nursery")
                    await self.cancel()
            else:
                log.exception(
                    f"{current_actor().uid} errored with {etype}, "
                    "cancelling actor nursery")
                await self.cancel()
        else:
            # XXX: this is effectively the lone cancellation/supervisor
            # strategy which exactly mimicks trio's behaviour
            log.debug(f"Waiting on subactors {self._children} to complete")
            try:
                await self.wait()
            except Exception as err:
                log.warn(f"Nursery caught {err}, cancelling")
                await self.cancel()
                raise
            log.debug(f"Nursery teardown complete")


@asynccontextmanager
async def open_nursery(supervisor=None):
    """Create and yield a new ``ActorNursery``.
    """
    actor = current_actor()
    if not actor:
        raise RuntimeError("No actor instance has been defined yet?")

    # TODO: figure out supervisors from erlang
    async with ActorNursery(current_actor(), supervisor) as nursery:
        yield nursery
