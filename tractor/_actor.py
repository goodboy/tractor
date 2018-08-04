"""
Actor primitives and helpers
"""
import inspect
import importlib
from collections import defaultdict
from functools import partial
import traceback
import uuid
from itertools import chain

import trio
from async_generator import asynccontextmanager, aclosing

from ._ipc import Channel, _connect_chan
from .log import get_console_log, get_logger
from ._portal import (Portal, open_portal, _do_handshake, LocalPortal,
                      maybe_open_nursery)
from . import _state
from ._state import current_actor


log = get_logger('tractor')


class ActorFailure(Exception):
    "General actor failure"


class InternalActorError(RuntimeError):
    "Actor primitive internals failure"


async def _invoke(
    actor, cid, chan, func, kwargs,
    treat_as_gen=False,
    task_status=trio.TASK_STATUS_IGNORED
):
    """Invoke local func and return results over provided channel.
    """
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
            with trio.open_cancel_scope() as cs:
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
                with trio.open_cancel_scope() as cs:
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
                await chan.send({'stop': None, 'cid': cid})
            else:
                if treat_as_gen:
                    await chan.send({'functype': 'asyncgen', 'cid': cid})
                    # XXX: the async-func may spawn further tasks which push
                    # back values like an async-generator would but must
                    # manualy construct the response dict-packet-responses as
                    # above
                    with trio.open_cancel_scope() as cs:
                        task_status.started(cs)
                        await coro
                else:
                    await chan.send({'functype': 'asyncfunction', 'cid': cid})
                    with trio.open_cancel_scope() as cs:
                        task_status.started(cs)
                        await chan.send({'return': await coro, 'cid': cid})
    except Exception:
        # always ship errors back to caller
        log.exception("Actor errored:")
        await chan.send({'error': traceback.format_exc(), 'cid': cid})
    finally:
        # RPC task bookeeping
        tasks = actor._rpc_tasks.get(chan, None)
        if tasks:
            tasks.remove((cs, func))

        if not tasks:
            actor._rpc_tasks.pop(chan, None)

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
        rpc_module_paths: [str] = [],
        statespace: dict = {},
        uid: str = None,
        allow_rpc: bool = True,
        loglevel: str = None,
        arbiter_addr: (str, int) = None,
    ):
        self.name = name
        self.uid = (name, uid or str(uuid.uuid1()))
        self.rpc_module_paths = rpc_module_paths
        self._mods = {}
        # TODO: consider making this a dynamically defined
        # @dataclass once we get py3.7
        self.statespace = statespace
        self._allow_rpc = allow_rpc
        self.loglevel = loglevel
        self._arb_addr = arbiter_addr

        # filled in by `_async_main` after fork
        self._root_nursery = None
        self._server_nursery = None
        self._peers = defaultdict(list)
        self._peer_connected = {}
        self._no_more_peers = trio.Event()
        self._no_more_peers.set()

        self._no_more_rpc_tasks = trio.Event()
        self._no_more_rpc_tasks.set()
        self._rpc_tasks = {}

        self._actors2calls = {}  # map {uids -> {callids -> waiter queues}}
        self._listeners = []
        self._parent_chan = None
        self._accept_host = None
        self._forkserver_info = None

    async def wait_for_peer(self, uid):
        """Wait for a connection back from a spawned actor with a given
        ``uid``.
        """
        log.debug(f"Waiting for peer {uid} to connect")
        event = self._peer_connected.setdefault(uid, trio.Event())
        await event.wait()
        log.debug(f"{uid} successfully connected back to us")
        return event, self._peers[uid][-1]

    def load_namespaces(self):
        # We load namespaces after fork since this actor may
        # be spawned on a different machine from the original nursery
        # and we need to try and load the local module code (if it
        # exists)
        for path in self.rpc_module_paths:
            self._mods[path] = importlib.import_module(path)

    async def _stream_handler(
        self,
        stream: trio.SocketStream,
    ):
        """
        Entry point for new inbound connections to the channel server.
        """
        self._no_more_peers.clear()
        chan = Channel(stream=stream)
        log.info(f"New connection to us {chan}")

        # send/receive initial handshake response
        try:
            uid = await _do_handshake(self, chan)
        except StopAsyncIteration:
            log.warn(f"Channel {chan} failed to handshake")
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
            log.warn(
                f"already have channel(s) for {uid}:{chans}?"
            )
        log.debug(f"Registered {chan} for {uid}")
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
                await chan.send(None)
                await chan.aclose()

    async def _push_result(self, actorid, cid, msg):
        assert actorid, f"`actorid` can't be {actorid}"
        q = self.get_waitq(actorid, cid)
        log.debug(f"Delivering {msg} from {actorid} to caller {cid}")
        # maintain backpressure
        await q.put(msg)

    def get_waitq(self, actorid, cid):
        log.debug(f"Getting result queue for {actorid} cid {cid}")
        cids2qs = self._actors2calls.setdefault(actorid, {})
        return cids2qs.setdefault(cid, trio.Queue(1000))

    async def send_cmd(self, chan, ns, func, kwargs):
        """Send a ``'cmd'`` message to a remote actor and return a
        caller id and a ``trio.Queue`` that can be used to wait for
        responses delivered by the local message processing loop.
        """
        cid = str(uuid.uuid1())
        q = self.get_waitq(chan.uid, cid)
        log.debug(f"Sending cmd to {chan.uid}: {ns}.{func}({kwargs})")
        await chan.send({'cmd': (ns, func, kwargs, self.uid, cid)})
        return cid, q

    async def _process_messages(self, chan, treat_as_gen=False):
        """Process messages async-RPC style.

        Process rpc requests and deliver retrieved responses from channels.
        """
        # TODO: once https://github.com/python-trio/trio/issues/467 gets
        # worked out we'll likely want to use that!
        log.debug(f"Entering msg loop for {chan} from {chan.uid}")
        try:
            async for msg in chan.aiter_recv():
                if msg is None:  # terminate sentinel
                    log.debug(
                        f"Cancelling all tasks for {chan} from {chan.uid}")
                    for scope, func in self._rpc_tasks.pop(chan, ()):
                        scope.cancel()

                    log.debug(
                            f"Msg loop signalled to terminate for"
                            f" {chan} from {chan.uid}")
                    break
                log.debug(f"Received msg {msg} from {chan.uid}")
                cid = msg.get('cid')
                if cid:
                    if cid == 'internal':  # internal actor error
                        # import pdb; pdb.set_trace()
                        raise InternalActorError(
                            f"{chan.uid}\n" + msg['error'])

                    # deliver response to local caller/waiter
                    await self._push_result(chan.uid, cid, msg)
                    log.debug(
                        f"Waiting on next msg for {chan} from {chan.uid}")
                    continue

                # process command request
                ns, funcname, kwargs, actorid, cid = msg['cmd']

                log.debug(
                    f"Processing request from {actorid}\n"
                    f"{ns}.{funcname}({kwargs})")
                if ns == 'self':
                    func = getattr(self, funcname)
                else:
                    func = getattr(self._mods[ns], funcname)

                # spin up a task for the requested function
                sig = inspect.signature(func)
                treat_as_gen = False
                if 'chan' in sig.parameters:
                    assert 'cid' in sig.parameters, \
                        f"{func} must accept a `cid` (caller id) kwarg"
                    kwargs['chan'] = chan
                    kwargs['cid'] = cid
                    # TODO: eventually we want to be more stringent
                    # about what is considered a far-end async-generator.
                    # Right now both actual async gens and any async
                    # function which declares a `chan` kwarg in its
                    # signature will be treated as one.
                    treat_as_gen = True

                log.debug(f"Spawning task for {func}")
                cs = await self._root_nursery.start(
                    _invoke, self, cid, chan, func, kwargs, treat_as_gen,
                    name=funcname
                )
                # never allow cancelling cancel requests (results in
                # deadlock and other weird behaviour)
                if func != self.cancel:
                    self._no_more_rpc_tasks.clear()
                    log.info(f"RPC func is {func}")
                    self._rpc_tasks.setdefault(chan, []).append((cs, func))
                log.debug(
                    f"Waiting on next msg for {chan} from {chan.uid}")
            else:  # channel disconnect
                log.debug(f"{chan} from {chan.uid} disconnected")

        except InternalActorError:
            # ship internal errors upwards
            if self._parent_chan:
                await self._parent_chan.send(
                    {'error': traceback.format_exc(), 'cid': 'internal'})
            raise
        except trio.ClosedResourceError:
            log.error(f"{chan} form {chan.uid} broke")
        except Exception:
            # ship exception (from above code) to peer as an internal error
            await chan.send(
                {'error': traceback.format_exc(), 'cid': 'internal'})
            raise
        finally:
            log.debug(f"Exiting msg loop for {chan} from {chan.uid}")

    def _fork_main(self, accept_addr, forkserver_info, parent_addr=None):
        # after fork routine which invokes a fresh ``trio.run``
        # log.warn("Log level after fork is {self.loglevel}")
        self._forkserver_info = forkserver_info
        from ._trionics import ctx
        if self.loglevel is not None:
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
        accept_addr,
        arbiter_addr=None,
        parent_addr=None,
        nursery=None,
        task_status=trio.TASK_STATUS_IGNORED,
    ):
        """Start the channel server, maybe connect back to the parent, and
        start the main task.

        A "root-most" (or "top-level") nursery for this actor is opened here
        and when cancelled effectively cancels the actor.
        """
        arbiter_addr = arbiter_addr or self._arb_addr
        registered_with_arbiter = False
        try:
            async with maybe_open_nursery(nursery) as nursery:
                self._root_nursery = nursery

                # Startup up channel server
                host, port = accept_addr
                await nursery.start(partial(
                    self._serve_forever, accept_host=host, accept_port=port)
                )

                # XXX: I wonder if a better name is maybe "requester"
                # since I don't think the notion of a "parent" actor
                # necessarily sticks given that eventually we want
                # ``'MainProcess'`` (the actor who initially starts the
                # forkserver) to eventually be the only one who is
                # allowed to spawn new processes per Python program.
                if parent_addr is not None:
                    try:
                        # Connect back to the parent actor and conduct initial
                        # handshake (From this point on if we error ship the
                        # exception back to the parent actor)
                        chan = self._parent_chan = Channel(
                            destaddr=parent_addr,
                        )
                        await chan.connect()
                        # initial handshake, report who we are, who they are
                        await _do_handshake(self, chan)
                    except OSError:  # failed to connect
                        log.warn(
                            f"Failed to connect to parent @ {parent_addr},"
                            " closing server")
                        await self.cancel()
                        self._parent_chan = None

                # register with the arbiter if we're told its addr
                log.debug(f"Registering {self} for role `{self.name}`")
                async with get_arbiter(*arbiter_addr) as arb_portal:
                    await arb_portal.run(
                        'self', 'register_actor',
                        name=self.name, sockaddr=self.accept_addr)
                    registered_with_arbiter = True

                task_status.started()
                # handle new connection back to parent optionally
                # begin responding to RPC
                if self._allow_rpc:
                    self.load_namespaces()
                    if self._parent_chan:
                        nursery.start_soon(
                            self._process_messages, self._parent_chan)

                log.debug("Waiting on root nursery to complete")
            # blocks here as expected if no nursery was provided until
            # the channel server is killed (i.e. this actor is
            # cancelled or signalled by the parent actor)
        except Exception:
            if self._parent_chan:
                try:
                    await self._parent_chan.send(
                        {'error': traceback.format_exc(), 'cid': 'internal'})
                except trio.ClosedStreamError:
                    log.error(
                        f"Failed to ship error to parent "
                        f"{self._parent_chan.uid}, channel was closed")
                    log.exception("Actor errored:")

            if not registered_with_arbiter:
                log.exception(
                    f"Failed to register with arbiter @ {arbiter_addr}")
            else:
                raise
        finally:
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
        accept_host=None,
        accept_port=0,
        task_status=trio.TASK_STATUS_IGNORED
    ):
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

    async def _do_unreg(self, arbiter_addr):
        # UNregister actor from the arbiter
        try:
            if arbiter_addr is not None:
                async with get_arbiter(*arbiter_addr) as arb_portal:
                    await arb_portal.run(
                        'self', 'unregister_actor', name=self.name)
        except OSError:
            log.warn(f"Unable to unregister {self.name} from arbiter")

    async def cancel(self):
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

    async def cancel_rpc_tasks(self):
        """Cancel all existing RPC responder tasks using the cancel scope
        registered for each.
        """
        scopes = self._rpc_tasks
        log.info(f"Cancelling all {len(scopes)} rpc tasks:\n{scopes}")
        for chan, scopes in scopes.items():
            log.debug(f"Cancelling all tasks for {chan.uid}")
            for scope, func in scopes:
                log.debug(f"Cancelling task for {func}")
                scope.cancel()
        if scopes:
            log.info(
                f"Waiting for remaining rpc tasks to complete {scopes}")
            await self._no_more_rpc_tasks.wait()

    def cancel_server(self):
        """Cancel the internal channel server nursery thereby
        preventing any new inbound connections from being established.
        """
        log.debug("Shutting down channel server")
        self._server_nursery.cancel_scope.cancel()

    @property
    def accept_addr(self):
        """Primary address to which the channel server is bound.
        """
        try:
            return self._listeners[0].socket.getsockname()
        except OSError:
            return

    def get_parent(self):
        return Portal(self._parent_chan)

    def get_chans(self, actorid):
        return self._peers[actorid]


class Arbiter(Actor):
    """A special actor who knows all the other actors and always has
    access to the top level nursery.

    The arbiter is by default the first actor spawned on each host
    and is responsible for keeping track of all other actors for
    coordination purposes. If a new main process is launched and an
    arbiter is already running that arbiter will be used.
    """
    _registry = defaultdict(list)
    is_arbiter = True

    def find_actor(self, name):
        return self._registry[name]

    def register_actor(self, name, sockaddr):
        self._registry[name].append(sockaddr)

    def unregister_actor(self, name):
        self._registry.pop(name, None)


async def _start_actor(actor, main, host, port, arbiter_addr, nursery=None):
    """Spawn a local actor by starting a task to execute it's main
    async function.

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
        if main is not None:
            result = await main()
            # XXX: If spawned with a dedicated "main function",
            # the actor is cancelled when this context is complete
            # given that there are no more active peer channels connected to it.
            actor.cancel_server()

    # block on actor to complete

    # unset module state
    _state._current_actor = None
    log.info("Completed async main")

    return result


@asynccontextmanager
async def get_arbiter(host, port):
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
    name,
    arbiter_sockaddr=None,
):
    """Ask the arbiter to find actor(s) by name.

    Returns a connected portal to the last registered matching actor
    known to the arbiter.
    """
    actor = current_actor()
    if not actor:
        raise RuntimeError("No actor instance has been defined yet?")

    async with get_arbiter(*arbiter_sockaddr or actor._arb_addr) as arb_portal:
        sockaddrs = await arb_portal.run('self', 'find_actor', name=name)
        # TODO: return portals to all available actors - for now just
        # the last one that registered
        if sockaddrs:
            sockaddr = sockaddrs[-1]
            async with _connect_chan(*sockaddr) as chan:
                async with open_portal(chan) as portal:
                    yield portal
        else:
            yield None
