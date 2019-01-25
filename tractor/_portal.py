"""
Portal api
"""
import importlib
import inspect
import typing
from typing import Tuple, Any, Dict, Optional, Set
from functools import partial
from dataclasses import dataclass

import trio
from async_generator import asynccontextmanager, aclosing

from ._state import current_actor
from ._ipc import Channel
from .log import get_logger
from ._exceptions import unpack_error, NoResult, RemoteActorError


log = get_logger('tractor')


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


async def _do_handshake(
    actor: 'Actor',  # type: ignore
    chan: Channel
)-> Any:
    await chan.send(actor.uid)
    uid: Tuple[str, str] = await chan.recv()

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

    Think of this like a native async IPC API.
    """
    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        # when this is set to a tuple returned from ``_submit()`` then
        # it is expected that ``result()`` will be awaited at some point
        # during the portal's lifetime
        self._result: Optional[Any] = None
        # set when _submit_for_result is called
        self._expect_result: Optional[
            Tuple[str, Any, str, Dict[str, Any]]
        ] = None
        self._agens: Set[typing.AsyncGenerator] = set()

    async def aclose(self) -> None:
        log.debug(f"Closing {self}")
        # XXX: won't work until https://github.com/python-trio/trio/pull/460
        # gets in!
        await self.channel.aclose()

    async def _submit(
        self, ns: str, func: str, **kwargs
    ) -> Tuple[str, trio.Queue, str, Dict[str, Any]]:
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
            raise unpack_error(first_msg, self.channel)
        else:
            raise ValueError(f"{first_msg} is an invalid response packet?")

        return cid, q, resp_type, first_msg

    async def _submit_for_result(self, ns: str, func: str, **kwargs) -> None:
        assert self._expect_result is None, \
                "A pending main result has already been submitted"
        self._expect_result = await self._submit(ns, func, **kwargs)

    async def run(self, ns: str, func: str, **kwargs) -> Any:
        """Submit a remote function to be scheduled and run by actor,
        wrap and return its (stream of) result(s).

        This is a blocking call and returns either a value from the
        remote rpc task or a local async generator instance.
        """
        return await self._return_from_resptype(
            *(await self._submit(ns, func, **kwargs))
        )

    async def _return_from_resptype(
        self, cid: str, q: trio.Queue, resptype: str, first_msg: dict
    ) -> Any:
        # TODO: not this needs some serious work and thinking about how
        # to make async-generators the fundamental IPC API over channels!
        # (think `yield from`, `gen.send()`, and functional reactive stuff)

        if resptype == 'yield':  # stream response

            async def yield_from_q():
                try:
                    async for msg in q:
                        try:
                            yield msg['yield']
                        except KeyError:
                            if 'stop' in msg:
                                break  # far end async gen terminated
                            else:
                                # internal error should never get here
                                assert msg.get('cid'), (
                                    "Received internal error at portal?")
                                raise unpack_error(msg, self.channel)

                except (GeneratorExit, trio.Cancelled):
                    log.warning(
                        f"Cancelling async gen call {cid} to "
                        f"{self.channel.uid}")
                    with trio.move_on_after(0.5) as cs:
                        cs.shield = True
                        # TODO: yeah.. it'd be nice if this was just an
                        # async func on the far end. Gotta figure out a
                        # better way then implicitly feeding the ctx
                        # to declaring functions; likely a decorator
                        # sytsem.
                        agen = await self.run('self', 'cancel_task', cid=cid)
                        async with aclosing(agen) as agen:
                            async for _ in agen:
                                pass
                    if cs.cancelled_caught:
                        if not self.channel.connected():
                            log.warning(
                                "May have failed to cancel remote task "
                                f"{cid} for {self.channel.uid}")
                    raise

            # TODO: use AsyncExitStack to aclose() all agens
            # on teardown
            agen = yield_from_q()
            self._agens.add(agen)
            return agen

        elif resptype == 'return':  # single response
            msg = await q.get()
            try:
                return msg['return']
            except KeyError:
                # internal error should never get here
                assert msg.get('cid'), "Received internal error at portal?"
                raise unpack_error(msg, self.channel)
        else:
            raise ValueError(f"Unknown msg response type: {first_msg}")

    async def result(self) -> Any:
        """Return the result(s) from the remote actor's "main" task.
        """
        # Check for non-rpc errors slapped on the
        # channel for which we always raise
        exc = self.channel._exc
        if exc:
            raise exc

        # not expecting a "main" result
        if self._expect_result is None:
            log.warning(
                f"Portal for {self.channel.uid} not expecting a final"
                " result?\nresult() should only be called if subactor"
                " was spawned with `ActorNursery.run_in_actor()`")
            return NoResult

        # expecting a "main" result
        assert self._expect_result
        if self._result is None:
            try:
                self._result = await self._return_from_resptype(
                    *self._expect_result
                )
            except RemoteActorError as err:
                self._result = err

        # re-raise error on every call
        if isinstance(self._result, RemoteActorError):
            raise self._result

        return self._result

    async def _cancel_streams(self):
        # terminate all locally running async generator
        # IPC calls
        if self._agens:
            log.warning(
                f"Cancelling all streams with {self.channel.uid}")
            for agen in self._agens:
                await agen.aclose()

    async def close(self) -> None:
        await self._cancel_streams()

    async def cancel_actor(self) -> bool:
        """Cancel the actor on the other end of this portal.
        """
        if not self.channel.connected():
            log.warning("This portal is already closed can't cancel")
            return False

        await self._cancel_streams()

        log.warning(
            f"Sending actor cancel request to {self.channel.uid} on "
            f"{self.channel}")
        try:
            # send cancel cmd - might not get response
            with trio.move_on_after(0.5) as cancel_scope:
                cancel_scope.shield = True
                await self.run('self', 'cancel')
                return True
            if cancel_scope.cancelled_caught:
                log.warning(f"May have failed to cancel {self.channel.uid}")
                return False
        except trio.ClosedResourceError:
            log.warning(
                f"{self.channel} for {self.channel.uid} was already closed?")
            return False


@dataclass
class LocalPortal:
    """A 'portal' to a local ``Actor``.

    A compatibility shim for normal portals but for invoking functions
    using an in process actor instance.
    """
    actor: 'Actor'  # type: ignore

    async def run(self, ns: str, func_name: str, **kwargs) -> Any:
        """Run a requested function locally and return it's result.
        """
        obj = self.actor if ns == 'self' else importlib.import_module(ns)
        func = getattr(obj, func_name)
        if inspect.iscoroutinefunction(func):
            return await func(**kwargs)
        else:
            return func(**kwargs)


@asynccontextmanager
async def open_portal(
    channel: Channel,
    nursery: trio._core._run.Nursery = None
) -> typing.AsyncGenerator[Portal, None]:
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

        msg_loop_cs = await nursery.start(
            partial(
                actor._process_messages,
                channel,
                # if the local task is cancelled we want to keep
                # the msg loop running until our block ends
                shield=True,
            )
        )
        portal = Portal(channel)
        try:
            yield portal
        finally:
            await portal.close()

            if was_connected:
                # cancel remote channel-msg loop
                await channel.send(None)

            # cancel background msg loop task
            msg_loop_cs.cancel()

            nursery.cancel_scope.cancel()
