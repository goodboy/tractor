"""
Portal api
"""
import importlib
import typing

import trio
from async_generator import asynccontextmanager

from ._state import current_actor
from .log import get_logger


log = get_logger('tractor')


class RemoteActorError(RuntimeError):
    "Remote actor exception bundled locally"


@asynccontextmanager
async def maybe_open_nursery(nursery: trio._core._run.Nursery = None):
    """Create a new nursery if None provided.

    Blocks on exit as expected if no input nursery is provided.
    """
    if nursery is not None:
        yield nursery
    else:
        async with trio.open_nursery() as nursery:
            yield nursery


async def _do_handshake(actor: 'Actor', chan: 'Channel') -> (str, str):
    await chan.send(actor.uid)
    uid = await chan.recv()

    if not isinstance(uid, tuple):
        raise ValueError(f"{uid} is not a valid uid?!")

    chan.uid = uid
    log.info(f"Handshake with actor {uid}@{chan.raddr} complete")
    return uid


class Portal:
    """A 'portal' to a(n) (remote) ``Actor``.

    Allows for invoking remote routines and receiving results through an
    underlying ``tractor.Channel`` as though the remote (async)
    function / generator was invoked locally.

    Think of this like an native async IPC API.
    """
    def __init__(self, channel: 'Channel'):
        self.channel = channel
        # when this is set to a tuple returned from ``_submit()`` then
        # it is expected that ``result()`` will be awaited at some point
        # during the portal's lifetime
        self._result = None
        self._exc = None
        self._expect_result = None

    async def aclose(self) -> None:
        log.debug(f"Closing {self}")
        # XXX: won't work until https://github.com/python-trio/trio/pull/460
        # gets in!
        await self.channel.aclose()

    async def _submit(
        self, ns: str, func: str, **kwargs
    ) -> (str, trio.Queue, str, dict):
        """Submit a function to be scheduled and run by actor, return the
        associated caller id, response queue, response type str,
        first message packet as a tuple.

        This is an async call.
        """
        # ship a function call request to the remote actor
        cid, q = await current_actor().send_cmd(self.channel, ns, func, kwargs)

        # wait on first response msg and handle (this should be
        # in an immediate response)
        first_msg = await q.get()
        functype = first_msg.get('functype')

        if functype == 'function' or functype == 'asyncfunction':
            resp_type = 'return'
        elif functype == 'asyncgen':
            resp_type = 'yield'
        elif 'error' in first_msg:
            raise RemoteActorError(
                f"{self.channel.uid}\n" + first_msg['error'])
        else:
            raise ValueError(f"{first_msg} is an invalid response packet?")

        return cid, q, resp_type, first_msg

    async def _submit_for_result(self, ns: str, func: str, **kwargs) -> None:
        assert self._expect_result is None, \
                "A pending main result has already been submitted"
        self._expect_result = await self._submit(ns, func, **kwargs)

    async def run(self, ns: str, func: str, **kwargs) -> typing.Any:
        """Submit a function to be scheduled and run by actor, wrap and return
        its (stream of) result(s).

        This is a blocking call.
        """
        return await self._return_from_resptype(
            *(await self._submit(ns, func, **kwargs))
        )

    async def _return_from_resptype(
        self, cid: str, q: trio.Queue, resptype: str, first_msg: dict
    ) -> typing.Any:
        # TODO: not this needs some serious work and thinking about how
        # to make async-generators the fundamental IPC API over channels!
        # (think `yield from`, `gen.send()`, and functional reactive stuff)

        if resptype == 'yield':

            async def yield_from_q():
                try:
                    async for msg in q:
                        try:
                            yield msg['yield']
                        except KeyError:
                            if 'stop' in msg:
                                break  # far end async gen terminated
                            else:
                                raise RemoteActorError(
                                    f"{self.channel.uid}\n" + msg['error'])
                except StopAsyncIteration:
                    log.debug(
                        f"Cancelling async gen call {cid} to "
                        f"{self.channel.uid}")
                    raise

            return yield_from_q()

        elif resptype == 'return':
            msg = await q.get()
            try:
                return msg['return']
            except KeyError:
                self._exc = RemoteActorError(
                    f"{self.channel.uid}\n" + msg['error'])
                raise self._exc
        else:
            raise ValueError(f"Unknown msg response type: {first_msg}")

    async def result(self) -> typing.Any:
        """Return the result(s) from the remote actor's "main" task.
        """
        if self._expect_result is None:
            # (remote) errors are slapped on the channel
            # teardown can reraise them
            exc = self.channel._exc
            if exc:
                raise RemoteActorError(f"{self.channel.uid}\n" + exc)
            else:
                raise RuntimeError(
                    f"Portal for {self.channel.uid} is not expecting a final"
                    "result?")

        elif self._result is None:
            self._result = await self._return_from_resptype(
                *self._expect_result
            )
        return self._result

    async def close(self) -> None:
        # trigger remote msg loop `break`
        chan = self.channel
        log.debug(f"Closing portal for {chan} to {chan.uid}")
        await self.channel.send(None)

    async def cancel_actor(self) -> bool:
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
        except trio.ClosedResourceError:
            log.warn(
                f"{self.channel} for {self.channel.uid} was already closed?")
            return False
        else:
            log.warn(f"May have failed to cancel {self.channel.uid}")
            return False


class LocalPortal:
    """A 'portal' to a local ``Actor``.

    A compatibility shim for normal portals but for invoking functions
    using an in process actor instance.
    """
    def __init__(self, actor: 'Actor'):
        self.actor = actor

    async def run(self, ns: str, func: str, **kwargs) -> typing.Any:
        """Run a requested function locally and return it's result.
        """
        obj = self.actor if ns == 'self' else importlib.import_module(ns)
        func = getattr(obj, func)
        return func(**kwargs)


@asynccontextmanager
async def open_portal(
    channel: 'Channel',
    nursery: trio._core._run.Nursery = None
) -> typing.AsyncContextManager[Portal]:
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
