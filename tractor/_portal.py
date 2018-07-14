"""
Portal api
"""
import importlib

import trio
from async_generator import asynccontextmanager

from ._state import current_actor
from .log import get_logger


log = get_logger('tractor')


class RemoteActorError(RuntimeError):
    "Remote actor exception bundled locally"


@asynccontextmanager
async def maybe_open_nursery(nursery=None):
    """Create a new nursery if None provided.

    Blocks on exit as expected if no input nursery is provided.
    """
    if nursery is not None:
        yield nursery
    else:
        async with trio.open_nursery() as nursery:
            yield nursery


async def _do_handshake(actor, chan):
    await chan.send(actor.uid)
    uid = await chan.recv()

    if not isinstance(uid, tuple):
        raise ValueError(f"{uid} is not a valid uid?!")

    chan.uid = uid
    log.info(f"Handshake with actor {uid}@{chan.raddr} complete")
    return uid


async def result_from_q(q, chan):
    """Process a msg from a remote actor.
    """
    first_msg = await q.get()
    if 'return' in first_msg:
        return 'return', first_msg, q
    elif 'yield' in first_msg:
        return 'yield', first_msg, q
    elif 'error' in first_msg:
        raise RemoteActorError(f"{chan.uid}\n" + first_msg['error'])
    else:
        raise ValueError(f"{first_msg} is an invalid response packet?")


class Portal:
    """A 'portal' to a(n) (remote) ``Actor``.

    Allows for invoking remote routines and receiving results through an
    underlying ``tractor.Channel`` as though the remote (async)
    function / generator was invoked locally.

    Think of this like an native async IPC API.
    """
    def __init__(self, channel):
        self.channel = channel
        self._result = None

    async def aclose(self):
        log.debug(f"Closing {self}")
        # XXX: won't work until https://github.com/python-trio/trio/pull/460
        # gets in!
        await self.channel.aclose()

    async def run(self, ns, func, **kwargs):
        """Submit a function to be scheduled and run by actor, return its
        (stream of) result(s).
        """
        # TODO: not this needs some serious work and thinking about how
        # to make async-generators the fundamental IPC API over channels!
        # (think `yield from`, `gen.send()`, and functional reactive stuff)
        actor = current_actor()
        # ship a function call request to the remote actor
        cid, q = await actor.send_cmd(self.channel, ns, func, kwargs)
        # wait on first response msg and handle
        return await self._return_from_resptype(
            cid, *(await result_from_q(q, self.channel)))

    async def _return_from_resptype(self, cid, resptype, first_msg, q):

        if resptype == 'yield':

            async def yield_from_q():
                yield first_msg['yield']
                try:
                    async for msg in q:
                        try:
                            yield msg['yield']
                        except KeyError:
                            if 'stop' in msg:
                                break  # far end async gen terminated
                            else:
                                raise RemoteActorError(msg['error'])
                except GeneratorExit:
                    log.debug(
                        f"Cancelling async gen call {cid} to "
                        f"{self.channel.uid}")
                    raise

            return yield_from_q()

        elif resptype == 'return':
            return first_msg['return']
        else:
            raise ValueError(f"Unknown msg response type: {first_msg}")

    async def result(self):
        """Return the result(s) from the remote actor's "main" task.
        """
        if self._result is None:
            q = current_actor().get_waitq(self.channel.uid, 'main')
            resptype, first_msg, q = (await result_from_q(q, self.channel))
            self._result = await self._return_from_resptype(
                'main', resptype, first_msg, q)
            log.warn(
                f"Retrieved first result `{self._result}` "
                f"for {self.channel.uid}")
        # await q.put(first_msg)  # for next consumer (e.g. nursery)
        return self._result

    async def close(self):
        # trigger remote msg loop `break`
        chan = self.channel
        log.debug(f"Closing portal for {chan} to {chan.uid}")
        await self.channel.send(None)

    async def cancel_actor(self):
        """Cancel the actor on the other end of this portal.
        """
        log.warn(
            f"Sending cancel request to {self.channel.uid} on "
            f"{self.channel}")
        try:
            with trio.move_on_after(0.1) as cancel_scope:
                cancel_scope.shield = True
                # send cancel cmd - might not get response
                await self.run('self', 'cancel')
                return True
        except trio.ClosedStreamError:
            log.warn(
                f"{self.channel} for {self.channel.uid} was already closed?")
            return False


class LocalPortal:
    """A 'portal' to a local ``Actor``.

    A compatibility shim for normal portals but for invoking functions
    using an in process actor instance.
    """
    def __init__(self, actor):
        self.actor = actor

    async def run(self, ns, func, **kwargs):
        """Run a requested function locally and return it's result.
        """
        obj = self.actor if ns == 'self' else importlib.import_module(ns)
        func = getattr(obj, func)
        return func(**kwargs)


@asynccontextmanager
async def open_portal(channel, nursery=None):
    """Open a ``Portal`` through the provided ``channel``.

    Spawns a background task to handle message processing.
    """
    actor = current_actor()
    assert actor
    was_connected = False

    async with maybe_open_nursery(nursery) as nursery:

        if not channel.connected():
            await channel.connect()
            was_connected = True

        if channel.uid is None:
            await _do_handshake(actor, channel)

        nursery.start_soon(actor._process_messages, channel)
        portal = Portal(channel)
        yield portal

        # cancel remote channel-msg loop
        if channel.connected():
            await portal.close()

        # cancel background msg loop task
        nursery.cancel_scope.cancel()
        if was_connected:
            await channel.aclose()
