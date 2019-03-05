"""
Actor primitives and helpers
"""
from collections import defaultdict
from functools import partial
from itertools import chain
import importlib
import inspect
import uuid
import typing
from typing import Dict, List, Tuple, Any, Optional, Union

import trio  # type: ignore
from async_generator import asynccontextmanager, aclosing

from ._ipc import Channel, _connect_chan, Context
from .log import get_console_log, get_logger
from ._exceptions import (
    pack_error,
    unpack_error,
    ModuleNotExposed
)
from ._portal import (
    Portal,
    open_portal,
    _do_handshake,
    LocalPortal,
)
from . import _state
from ._state import current_actor


log = get_logger('tractor')


class ActorFailure(Exception):
    "General actor failure"


async def _invoke(
    actor: 'Actor',
    cid: str,
    chan: Channel,
    func: typing.Callable,
    kwargs: Dict[str, Any],
    task_status=trio.TASK_STATUS_IGNORED
):
    """Invoke local func and return results over provided channel.
    """
    sig = inspect.signature(func)
    treat_as_gen = False
    cs = None
    ctx = Context(chan, cid)
    if 'ctx' in sig.parameters:
        kwargs['ctx'] = ctx
        # TODO: eventually we want to be more stringent
        # about what is considered a far-end async-generator.
        # Right now both actual async gens and any async
        # function which declares a `ctx` kwarg in its
        # signature will be treated as one.
        treat_as_gen = True
    try:
        is_async_partial = False
        is_async_gen_partial = False
        if isinstance(func, partial):
            is_async_partial = inspect.iscoroutinefunction(func.func)
            is_async_gen_partial = inspect.isasyncgenfunction(func.func)

        if (
            not inspect.iscoroutinefunction(func) and
            not inspect.isasyncgenfunction(func) and
            not is_async_partial and
            not is_async_gen_partial
        ):
            await chan.send({'functype': 'function', 'cid': cid})
            with trio.CancelScope() as cs:
                task_status.started(cs)
                await chan.send({'return': func(**kwargs), 'cid': cid})
        else:
            coro = func(**kwargs)

            if inspect.isasyncgen(coro):
                await chan.send({'functype': 'asyncgen', 'cid': cid})
                # XXX: massive gotcha! If the containing scope
                # is cancelled and we execute the below line,
                # any ``ActorNursery.__aexit__()`` WON'T be
                # triggered in the underlying async gen! So we
                # have to properly handle the closing (aclosing)
                # of the async gen in order to be sure the cancel
                # is propagated!
                with trio.CancelScope() as cs:
                    task_status.started(cs)
                    async with aclosing(coro) as agen:
                        async for item in agen:
                            # TODO: can we send values back in here?
                            # it's gonna require a `while True:` and
                            # some non-blocking way to retrieve new `asend()`
                            # values from the channel:
                            # to_send = await chan.recv_nowait()
                            # if to_send is not None:
                            #     to_yield = await coro.asend(to_send)
                            await chan.send({'yield': item, 'cid': cid})

                log.debug(f"Finished iterating {coro}")
                # TODO: we should really support a proper
                # `StopAsyncIteration` system here for returning a final
                # value if desired
                await chan.send({'stop': True, 'cid': cid})
            else:
                if treat_as_gen:
                    await chan.send({'functype': 'asyncgen', 'cid': cid})
                    # XXX: the async-func may spawn further tasks which push
                    # back values like an async-generator would but must
                    # manualy construct the response dict-packet-responses as
                    # above
                    with trio.CancelScope() as cs:
                        task_status.started(cs)
                        await coro
                    if not cs.cancelled_caught:
                        # task was not cancelled so we can instruct the
                        # far end async gen to tear down
                        await chan.send({'stop': True, 'cid': cid})
                else:
                    await chan.send({'functype': 'asyncfunction', 'cid': cid})
                    with trio.CancelScope() as cs:
                        task_status.started(cs)
                        await chan.send({'return': await coro, 'cid': cid})
    except Exception as err:
        # always ship errors back to caller
        log.exception("Actor errored:")
        err_msg = pack_error(err)
        err_msg['cid'] = cid
        try:
            await chan.send(err_msg)
        except trio.ClosedResourceError:
            log.exception(
                f"Failed to ship error to caller @ {chan.uid}")
        if cs is None:
            # error is from above code not from rpc invocation
            task_status.started(err)
    finally:
        # RPC task bookeeping
        try:
            scope, func, is_complete = actor._rpc_tasks.pop((chan, cid))
            is_complete.set()
        except KeyError:
            # If we're cancelled before the task returns then the
            # cancel scope will not have been inserted yet
            log.warn(
                f"Task {func} was likely cancelled before it was started")

        if not actor._rpc_tasks:
            log.info(f"All RPC tasks have completed")
            actor._no_more_rpc_tasks.set()


class Actor:
    """The fundamental concurrency primitive.

    An *actor* is the combination of a regular Python or
    ``multiprocessing.Process`` executing a ``trio`` task tree, communicating
    with other actors through "portals" which provide a native async API
    around "channels".
    """
    is_arbiter = False

    def __init__(
        self,
        name: str,
        rpc_module_paths: List[str] = [],
        statespace: Optional[Dict[str, Any]] = None,
        uid: str = None,
        loglevel: str = None,
        arbiter_addr: Optional[Tuple[str, int]] = None,
    ) -> None:
        self.name = name
        self.uid = (name, uid or str(uuid.uuid1()))
        self.rpc_module_paths = rpc_module_paths
        self._mods: dict = {}
        # TODO: consider making this a dynamically defined
        # @dataclass once we get py3.7
        self.statespace = statespace or {}
        self.loglevel = loglevel
        self._arb_addr = arbiter_addr

        # filled in by `_async_main` after fork
        self._root_nursery: trio._core._run.Nursery = None
        self._server_nursery: trio._core._run.Nursery = None
        self._peers: defaultdict = defaultdict(list)
        self._peer_connected: dict = {}
        self._no_more_peers = trio.Event()
        self._no_more_peers.set()

        self._no_more_rpc_tasks = trio.Event()
        self._no_more_rpc_tasks.set()
        # (chan, cid) -> (cancel_scope, func)
        self._rpc_tasks: Dict[
            Tuple[Channel, str],
            Tuple[trio._core._run.CancelScope, typing.Callable, trio.Event]
        ] = {}
        # map {uids -> {callids -> waiter queues}}
        self._cids2qs: Dict[
            Tuple[Tuple[str, str], str],
            trio.abc.SendChannel[Any]] = {}
        self._listeners: List[trio.abc.Listener] = []
        self._parent_chan: Optional[Channel] = None
        self._forkserver_info: Optional[Tuple[Any, Any, Any, Any, Any]] = None

    async def wait_for_peer(
        self, uid: Tuple[str, str]
    ) -> Tuple[trio.Event, Channel]:
        """Wait for a connection back from a spawned actor with a given
        ``uid``.
        """
        log.debug(f"Waiting for peer {uid} to connect")
        event = self._peer_connected.setdefault(uid, trio.Event())
        await event.wait()
        log.debug(f"{uid} successfully connected back to us")
        return event, self._peers[uid][-1]

    def load_modules(self) -> None:
        """Load allowed RPC modules locally (after fork).

        Since this actor may be spawned on a different machine from
        the original nursery we need to try and load the local module
        code (if it exists).
        """
        for path in self.rpc_module_paths:
            log.debug(f"Attempting to import {path}")
            self._mods[path] = importlib.import_module(path)

    def _get_rpc_func(self, ns, funcname):
        try:
            return getattr(self._mods[ns], funcname)
        except KeyError as err:
            raise ModuleNotExposed(*err.args)

    async def _stream_handler(
        self,
        stream: trio.SocketStream,
    ) -> None:
        """Entry point for new inbound connections to the channel server.
        """
        self._no_more_peers.clear()
        chan = Channel(stream=stream)
        log.info(f"New connection to us {chan}")

        # send/receive initial handshake response
        try:
            uid = await _do_handshake(self, chan)
        except StopAsyncIteration:
            log.warning(f"Channel {chan} failed to handshake")
            return

        # channel tracking
        event = self._peer_connected.pop(uid, None)
        if event:
            # Instructing connection: this is likely a new channel to
            # a recently spawned actor which we'd like to control via
            # async-rpc calls.
            log.debug(f"Waking channel waiters {event.statistics()}")
            # Alert any task waiting on this connection to come up
            event.set()

        chans = self._peers[uid]
        if chans:
            log.warning(
                f"already have channel(s) for {uid}:{chans}?"
            )
        log.trace(f"Registered {chan} for {uid}")  # type: ignore
        # append new channel
        self._peers[uid].append(chan)

        # Begin channel management - respond to remote requests and
        # process received reponses.
        try:
            await self._process_messages(chan)
        finally:
            # Drop ref to channel so it can be gc-ed and disconnected
            log.debug(f"Releasing channel {chan} from {chan.uid}")
            chans = self._peers.get(chan.uid)
            chans.remove(chan)
            if not chans:
                log.debug(f"No more channels for {chan.uid}")
                self._peers.pop(chan.uid, None)

            log.debug(f"Peers is {self._peers}")

            if not self._peers:  # no more channels connected
                self._no_more_peers.set()
                log.debug(f"Signalling no more peer channels")

            # # XXX: is this necessary (GC should do it?)
            if chan.connected():
                log.debug(f"Disconnecting channel {chan}")
                try:
                    # send our msg loop terminate sentinel
                    await chan.send(None)
                    # await chan.aclose()
                except trio.BrokenResourceError:
                    log.exception(
                        f"Channel for {chan.uid} was already zonked..")

    async def _push_result(
        self,
        chan: Channel,
        msg: Dict[str, Any],
    ) -> None:
        """Push an RPC result to the local consumer's queue.
        """
        actorid = chan.uid
        assert actorid, f"`actorid` can't be {actorid}"
        cid = msg['cid']
        send_chan = self._cids2qs[(actorid, cid)]
        assert send_chan.cid == cid
        if 'stop' in msg:
            log.debug(f"{send_chan} was terminated at remote end")
            return await send_chan.aclose()
        try:
            log.debug(f"Delivering {msg} from {actorid} to caller {cid}")
            # maintain backpressure
            await send_chan.send(msg)
        except trio.BrokenResourceError:
            # XXX: local consumer has closed their side
            # so cancel the far end streaming task
            log.warning(f"{send_chan} consumer is already closed")

    def get_memchans(
        self,
        actorid: Tuple[str, str],
        cid: str
    ) -> trio.abc.ReceiveChannel:
        log.debug(f"Getting result queue for {actorid} cid {cid}")
        try:
            recv_chan = self._cids2qs[(actorid, cid)]
        except KeyError:
            send_chan, recv_chan = trio.open_memory_channel(1000)
            send_chan.cid = cid
            self._cids2qs[(actorid, cid)] = send_chan

        return recv_chan

    async def send_cmd(
        self,
        chan: Channel,
        ns: str,
        func: str,
        kwargs: dict
    ) -> Tuple[str, trio.abc.ReceiveChannel]:
        """Send a ``'cmd'`` message to a remote actor and return a
        caller id and a ``trio.Queue`` that can be used to wait for
        responses delivered by the local message processing loop.
        """
        cid = str(uuid.uuid1())
        assert chan.uid
        recv_chan = self.get_memchans(chan.uid, cid)
        log.debug(f"Sending cmd to {chan.uid}: {ns}.{func}({kwargs})")
        await chan.send({'cmd': (ns, func, kwargs, self.uid, cid)})
        return cid, recv_chan

    async def _process_messages(
        self, chan: Channel,
        treat_as_gen: bool = False,
        shield: bool = False,
        task_status=trio.TASK_STATUS_IGNORED,
    ) -> None:
        """Process messages for the channel async-RPC style.

        Receive multiplexed RPC requests and deliver responses over ``chan``.
        """
        # TODO: once https://github.com/python-trio/trio/issues/467 gets
        # worked out we'll likely want to use that!
        msg = None
        log.debug(f"Entering msg loop for {chan} from {chan.uid}")
        try:
            # internal scope allows for keeping this message
            # loop running despite the current task having been
            # cancelled (eg. `open_portal()` may call this method from
            # a locally spawned task)
            with trio.CancelScope(shield=shield) as cs:
                task_status.started(cs)
                async for msg in chan:
                    if msg is None:  # loop terminate sentinel
                        log.debug(
                            f"Cancelling all tasks for {chan} from {chan.uid}")
                        for (channel, cid) in self._rpc_tasks:
                            if channel is chan:
                                self.cancel_task(cid, Context(channel, cid))
                        log.debug(
                                f"Msg loop signalled to terminate for"
                                f" {chan} from {chan.uid}")
                        break

                    log.trace(f"Received msg {msg} from {chan.uid}")  # type: ignore
                    if msg.get('cid'):
                        # deliver response to local caller/waiter
                        await self._push_result(chan, msg)
                        log.debug(
                            f"Waiting on next msg for {chan} from {chan.uid}")
                        continue

                    # process command request
                    try:
                        ns, funcname, kwargs, actorid, cid = msg['cmd']
                    except KeyError:
                        # This is the non-rpc error case, that is, an
                        # error **not** raised inside a call to ``_invoke()``
                        # (i.e. no cid was provided in the msg - see above).
                        # Push this error to all local channel consumers
                        # (normally portals) by marking the channel as errored
                        assert chan.uid
                        exc = unpack_error(msg, chan=chan)
                        chan._exc = exc
                        raise exc

                    log.debug(
                        f"Processing request from {actorid}\n"
                        f"{ns}.{funcname}({kwargs})")
                    if ns == 'self':
                        func = getattr(self, funcname)
                    else:
                        # complain to client about restricted modules
                        try:
                            func = self._get_rpc_func(ns, funcname)
                        except (ModuleNotExposed, AttributeError) as err:
                            err_msg = pack_error(err)
                            err_msg['cid'] = cid
                            await chan.send(err_msg)
                            continue

                    # spin up a task for the requested function
                    log.debug(f"Spawning task for {func}")
                    cs = await self._root_nursery.start(
                        _invoke, self, cid, chan, func, kwargs,
                        name=funcname
                    )
                    # never allow cancelling cancel requests (results in
                    # deadlock and other weird behaviour)
                    if func != self.cancel:
                        if isinstance(cs, Exception):
                            log.warn(f"Task for RPC func {func} failed with"
                                     f"{cs}")
                        else:
                            # mark that we have ongoing rpc tasks
                            self._no_more_rpc_tasks.clear()
                            log.info(f"RPC func is {func}")
                            # store cancel scope such that the rpc task can be
                            # cancelled gracefully if requested
                            self._rpc_tasks[(chan, cid)] = (
                                cs, func, trio.Event())
                    log.debug(
                        f"Waiting on next msg for {chan} from {chan.uid}")
                else:
                    # channel disconnect
                    log.debug(f"{chan} from {chan.uid} disconnected")

        except trio.ClosedResourceError:
            log.error(f"{chan} form {chan.uid} broke")
        except Exception as err:
            # ship any "internal" exception (i.e. one from internal machinery
            # not from an rpc task) to parent
            log.exception("Actor errored:")
            if self._parent_chan:
                await self._parent_chan.send(pack_error(err))
            raise
            # if this is the `MainProcess` we expect the error broadcasting
            # above to trigger an error at consuming portal "checkpoints"
        except trio.Cancelled:
            # debugging only
            log.debug("Msg loop was cancelled")
            raise
        finally:
            log.debug(
                f"Exiting msg loop for {chan} from {chan.uid} "
                f"with last msg:\n{msg}")

    def _fork_main(
        self,
        accept_addr: Tuple[str, int],
        forkserver_info: Tuple[Any, Any, Any, Any, Any],
        parent_addr: Tuple[str, int] = None
    ) -> None:
        """The routine called *after fork* which invokes a fresh ``trio.run``
        """
        self._forkserver_info = forkserver_info
        from ._spawn import ctx
        if self.loglevel is not None:
            log.info(
                f"Setting loglevel for {self.uid} to {self.loglevel}")
            get_console_log(self.loglevel)
        log.info(
            f"Started new {ctx.current_process()} for {self.uid}")
        _state._current_actor = self
        log.debug(f"parent_addr is {parent_addr}")
        try:
            trio.run(partial(
                self._async_main, accept_addr, parent_addr=parent_addr))
        except KeyboardInterrupt:
            pass  # handle it the same way trio does?
        log.info(f"Actor {self.uid} terminated")

    async def _async_main(
        self,
        accept_addr: Tuple[str, int],
        arbiter_addr: Optional[Tuple[str, int]] = None,
        parent_addr: Optional[Tuple[str, int]] = None,
        task_status: trio._core._run._TaskStatus = trio.TASK_STATUS_IGNORED,
    ) -> None:
        """Start the channel server, maybe connect back to the parent, and
        start the main task.

        A "root-most" (or "top-level") nursery for this actor is opened here
        and when cancelled effectively cancels the actor.
        """
        arbiter_addr = arbiter_addr or self._arb_addr
        registered_with_arbiter = False
        try:
            async with trio.open_nursery() as nursery:
                self._root_nursery = nursery

                # Startup up channel server
                host, port = accept_addr
                await nursery.start(partial(
                    self._serve_forever, accept_host=host, accept_port=port)
                )

                if parent_addr is not None:
                    try:
                        # Connect back to the parent actor and conduct initial
                        # handshake (From this point on if we error, ship the
                        # exception back to the parent actor)
                        chan = self._parent_chan = Channel(
                            destaddr=parent_addr,
                        )
                        await chan.connect()
                        # initial handshake, report who we are, who they are
                        await _do_handshake(self, chan)
                    except OSError:  # failed to connect
                        log.warning(
                            f"Failed to connect to parent @ {parent_addr},"
                            " closing server")
                        await self.cancel()
                        self._parent_chan = None

                    # handle new connection back to parent
                    nursery.start_soon(
                        self._process_messages, self._parent_chan)

                # load exposed/allowed RPC modules
                # XXX: do this **after** establishing connection to parent
                # so that import errors are properly propagated upwards
                self.load_modules()

                # register with the arbiter if we're told its addr
                log.debug(f"Registering {self} for role `{self.name}`")
                async with get_arbiter(*arbiter_addr) as arb_portal:
                    await arb_portal.run(
                        'self', 'register_actor',
                        uid=self.uid, sockaddr=self.accept_addr)
                    registered_with_arbiter = True

                task_status.started()
                log.debug("Waiting on root nursery to complete")

            # blocks here as expected until the channel server is
            # killed (i.e. this actor is cancelled or signalled by the parent)
        except Exception as err:
            if not registered_with_arbiter:
                log.exception(
                    f"Actor errored and failed to register with arbiter "
                    f"@ {arbiter_addr}")

            if self._parent_chan:
                try:
                    # internal error so ship to parent without cid
                    await self._parent_chan.send(pack_error(err))
                except trio.ClosedResourceError:
                    log.error(
                        f"Failed to ship error to parent "
                        f"{self._parent_chan.uid}, channel was closed")
                    log.exception("Actor errored:")
            else:
                # XXX wait, why?
                # causes a hang if I always raise..
                raise

        finally:
            if registered_with_arbiter:
                await self._do_unreg(arbiter_addr)
            # terminate actor once all it's peers (actors that connected
            # to it as clients) have disappeared
            if not self._no_more_peers.is_set():
                if any(
                    chan.connected() for chan in chain(*self._peers.values())
                ):
                    log.debug(
                        f"Waiting for remaining peers {self._peers} to clear")
                    await self._no_more_peers.wait()
            log.debug(f"All peer channels are complete")

            # tear down channel server no matter what since we errored
            # or completed
            self.cancel_server()

    async def _serve_forever(
        self,
        *,
        # (host, port) to bind for channel server
        accept_host: Tuple[str, int] = None,
        accept_port: int = 0,
        task_status: trio._core._run._TaskStatus = trio.TASK_STATUS_IGNORED,
    ) -> None:
        """Start the channel server, begin listening for new connections.

        This will cause an actor to continue living (blocking) until
        ``cancel_server()`` is called.
        """
        async with trio.open_nursery() as nursery:
            self._server_nursery = nursery
            # TODO: might want to consider having a separate nursery
            # for the stream handler such that the server can be cancelled
            # whilst leaving existing channels up
            listeners = await nursery.start(
                partial(
                    trio.serve_tcp,
                    self._stream_handler,
                    # new connections will stay alive even if this server
                    # is cancelled
                    handler_nursery=self._root_nursery,
                    port=accept_port, host=accept_host,
                )
            )
            log.debug(
                f"Started tcp server(s) on {[l.socket for l in listeners]}")
            self._listeners.extend(listeners)
            task_status.started()

    async def _do_unreg(self, arbiter_addr: Optional[Tuple[str, int]]) -> None:
        # UNregister actor from the arbiter
        try:
            if arbiter_addr is not None:
                async with get_arbiter(*arbiter_addr) as arb_portal:
                    await arb_portal.run(
                        'self', 'unregister_actor', uid=self.uid)
        except OSError:
            log.warning(f"Unable to unregister {self.name} from arbiter")

    async def cancel(self) -> None:
        """Cancel this actor.

        The sequence in order is:
            - cancelling all rpc tasks
            - cancelling the channel server
            - cancel the "root" nursery
        """
        # cancel all ongoing rpc tasks
        await self.cancel_rpc_tasks()
        self.cancel_server()
        self._root_nursery.cancel_scope.cancel()

    async def cancel_task(self, cid, ctx):
        """Cancel a local task.

        Note this method will be treated as a streaming funciton
        by remote actor-callers due to the declaration of ``ctx``
        in the signature (for now).
        """
        # right now this is only implicitly called by
        # streaming IPC but it should be called
        # to cancel any remotely spawned task
        chan = ctx.chan
        try:
            # this ctx based lookup ensures the requested task to
            # be cancelled was indeed spawned by a request from this channel
            scope, func, is_complete = self._rpc_tasks[(ctx.chan, cid)]
        except KeyError:
            log.warning(f"{cid} has already completed/terminated?")
            return

        log.debug(
            f"Cancelling task:\ncid: {cid}\nfunc: {func}\n"
            f"peer: {chan.uid}\n")

        # don't allow cancelling this function mid-execution
        # (is this necessary?)
        if func is self.cancel_task:
            return

        scope.cancel()
        # wait for _invoke to mark the task complete
        await is_complete.wait()
        log.debug(
            f"Sucessfully cancelled task:\ncid: {cid}\nfunc: {func}\n"
            f"peer: {chan.uid}\n")

    async def cancel_rpc_tasks(self) -> None:
        """Cancel all existing RPC responder tasks using the cancel scope
        registered for each.
        """
        tasks = self._rpc_tasks
        log.info(f"Cancelling all {len(tasks)} rpc tasks:\n{tasks} ")
        for (chan, cid) in tasks.copy():
            # TODO: this should really done in a nursery batch
            await self.cancel_task(cid, Context(chan, cid))
        # if tasks:
        log.info(
            f"Waiting for remaining rpc tasks to complete {tasks}")
        await self._no_more_rpc_tasks.wait()

    def cancel_server(self) -> None:
        """Cancel the internal channel server nursery thereby
        preventing any new inbound connections from being established.
        """
        log.debug("Shutting down channel server")
        self._server_nursery.cancel_scope.cancel()

    @property
    def accept_addr(self) -> Optional[Tuple[str, int]]:
        """Primary address to which the channel server is bound.
        """
        try:
            return self._listeners[0].socket.getsockname()
        except OSError:
            return None

    def get_parent(self) -> Portal:
        """Return a portal to our parent actor."""
        assert self._parent_chan, "No parent channel for this actor?"
        return Portal(self._parent_chan)

    def get_chans(self, uid: Tuple[str, str]) -> List[Channel]:
        """Return all channels to the actor with provided uid."""
        return self._peers[uid]


class Arbiter(Actor):
    """A special actor who knows all the other actors and always has
    access to a top level nursery.

    The arbiter is by default the first actor spawned on each host
    and is responsible for keeping track of all other actors for
    coordination purposes. If a new main process is launched and an
    arbiter is already running that arbiter will be used.
    """
    is_arbiter = True

    def __init__(self, *args, **kwargs):
        self._registry = defaultdict(list)
        self._waiters = {}
        super().__init__(*args, **kwargs)

    def find_actor(self, name: str) -> Optional[Tuple[str, int]]:
        for uid, sockaddr in self._registry.items():
            if name in uid:
                return sockaddr

        return None

    async def wait_for_actor(
        self, name: str
    ) -> List[Tuple[str, int]]:
        """Wait for a particular actor to register.

        This is a blocking call if no actor by the provided name is currently
        registered.
        """
        sockaddrs = []

        for (aname, _), sockaddr in self._registry.items():
            if name == aname:
                sockaddrs.append(sockaddr)

        if not sockaddrs:
            waiter = trio.Event()
            self._waiters.setdefault(name, []).append(waiter)
            await waiter.wait()
            for uid in self._waiters[name]:
                sockaddrs.append(self._registry[uid])

        return sockaddrs

    def register_actor(
        self, uid: Tuple[str, str], sockaddr: Tuple[str, int]
    ) -> None:
        name, uuid = uid
        self._registry[uid] = sockaddr

        # pop and signal all waiter events
        events = self._waiters.pop(name, ())
        self._waiters.setdefault(name, []).append(uid)
        for event in events:
            if isinstance(event, trio.Event):
                event.set()

    def unregister_actor(self, uid: Tuple[str, str]) -> None:
        self._registry.pop(uid, None)


async def _start_actor(
    actor: Actor,
    main: typing.Callable[..., typing.Awaitable],
    host: str,
    port: int,
    arbiter_addr: Tuple[str, int],
    nursery: trio._core._run.Nursery = None
):
    """Spawn a local actor by starting a task to execute it's main async
    function.

    Blocks if no nursery is provided, in which case it is expected the nursery
    provider is responsible for waiting on the task to complete.
    """
    # assign process-local actor
    _state._current_actor = actor

    # start local channel-server and fake the portal API
    # NOTE: this won't block since we provide the nursery
    log.info(f"Starting local {actor} @ {host}:{port}")

    async with trio.open_nursery() as nursery:
        await nursery.start(
            partial(
                actor._async_main,
                accept_addr=(host, port),
                parent_addr=None,
                arbiter_addr=arbiter_addr,
            )
        )
        result = await main()

        # XXX: the actor is cancelled when this context is complete
        # given that there are no more active peer channels connected
        actor.cancel_server()

    # unset module state
    _state._current_actor = None
    log.info("Completed async main")

    return result


@asynccontextmanager
async def get_arbiter(
    host: str, port: int
) -> typing.AsyncGenerator[Union[Portal, LocalPortal], None]:
    """Return a portal instance connected to a local or remote
    arbiter.
    """
    actor = current_actor()
    if not actor:
        raise RuntimeError("No actor instance has been defined yet?")

    if actor.is_arbiter:
        # we're already the arbiter
        # (likely a re-entrant call from the arbiter actor)
        yield LocalPortal(actor)
    else:
        async with _connect_chan(host, port) as chan:
            async with open_portal(chan) as arb_portal:
                yield arb_portal


@asynccontextmanager
async def find_actor(
    name: str, arbiter_sockaddr: Tuple[str, int] = None
) -> typing.AsyncGenerator[Optional[Portal], None]:
    """Ask the arbiter to find actor(s) by name.

    Returns a connected portal to the last registered matching actor
    known to the arbiter.
    """
    actor = current_actor()
    async with get_arbiter(*arbiter_sockaddr or actor._arb_addr) as arb_portal:
        sockaddr = await arb_portal.run('self', 'find_actor', name=name)
        # TODO: return portals to all available actors - for now just
        # the last one that registered
        if name == 'arbiter' and actor.is_arbiter:
            raise RuntimeError("The current actor is the arbiter")
        elif sockaddr:
            async with _connect_chan(*sockaddr) as chan:
                async with open_portal(chan) as portal:
                    yield portal
        else:
            yield None


@asynccontextmanager
async def wait_for_actor(
    name: str,
    arbiter_sockaddr: Tuple[str, int] = None
) -> typing.AsyncGenerator[Portal, None]:
    """Wait on an actor to register with the arbiter.

    A portal to the first registered actor is returned.
    """
    actor = current_actor()
    async with get_arbiter(*arbiter_sockaddr or actor._arb_addr) as arb_portal:
        sockaddrs = await arb_portal.run('self', 'wait_for_actor', name=name)
        sockaddr = sockaddrs[-1]
        async with _connect_chan(*sockaddr) as chan:
            async with open_portal(chan) as portal:
                yield portal
