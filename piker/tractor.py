"""
tracor: An actor model micro-framework.
"""
from collections import defaultdict
from functools import partial
from typing import Coroutine
import importlib
import inspect
import multiprocessing as mp
import traceback
import uuid

import trio
from async_generator import asynccontextmanager

from .ipc import Channel
from .log import get_console_log, get_logger


ctx = mp.get_context("forkserver")
log = get_logger('tractor')

# set at startup and after forks
_current_actor = None

# for debugging
log = get_console_log('info')


class ActorFailure(Exception):
    "General actor failure"


class RemoteActorError(ActorFailure):
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


async def _invoke(
    cid, chan, func, kwargs,
    treat_as_gen=False, raise_errs=False,
    task_status=trio.TASK_STATUS_IGNORED
):
    """Invoke local func and return results over provided channel.
    """
    try:
        is_async_func = False
        if isinstance(func, partial):
            is_async_func = inspect.iscoroutinefunction(func.func)

        if not inspect.iscoroutinefunction(func) and not is_async_func:
            await chan.send({'return': func(**kwargs), 'cid': cid})
        else:
            coro = func(**kwargs)

            if inspect.isasyncgen(coro):
                # await chan.send('gen')
                async for item in coro:
                    # TODO: can we send values back in here?
                    # How do we do it, spawn another task?
                    # to_send = await chan.recv()
                    # if to_send is not None:
                    #     await coro.send(to_send)
                    await chan.send({'yield': item, 'cid': cid})
            else:
                if treat_as_gen:
                    # XXX: the async-func may spawn further tasks which push
                    # back values like an async-generator would but must
                    # manualy construct the response dict-packet-responses as
                    # above
                    await coro
                else:
                    await chan.send({'return': await coro, 'cid': cid})

        task_status.started()
    except Exception:
        if not raise_errs:
            await chan.send({'error': traceback.format_exc(), 'cid': cid})
        else:
            raise


async def result_from_q(q):
    """Process a msg from a remote actor.
    """
    first_msg = await q.get()
    if 'return' in first_msg:
        return 'return', first_msg, q
    elif 'yield' in first_msg:
        return 'yield', first_msg, q
    elif 'error' in first_msg:
        raise RemoteActorError(first_msg['error'])
    else:
        raise ValueError(f"{first_msg} is an invalid response packet?")


class Actor:
    """The fundamental concurrency primitive.

    An actor is the combination of a ``multiprocessing.Process``
    executing a ``trio`` task tree, communicating with other actors
    through "portals" which provide a native async API around "channels".
    """
    is_arbiter = False

    def __init__(
        self,
        name: str,
        main: Coroutine = None,
        rpc_module_paths: [str] = [],
        statespace: dict = {},
        uid: str = None,
        allow_rpc: bool = True,
        outlive_main: bool = False,
    ):
        self.uid = (name, uid or str(uuid.uuid1()))
        self.rpc_module_paths = rpc_module_paths
        self._mods = {}
        self.main = main
        # TODO: consider making this a dynamically defined
        # @dataclass once we get py3.7
        self.statespace = statespace
        self._allow_rpc = allow_rpc
        self._outlive_main = outlive_main

        # filled in by `_async_main` after fork
        self._peers = {}
        self._no_more_peers = trio.Event()
        self._no_more_peers.set()
        self._actors2calls = {}  # map {uids -> {callids -> waiter queues}}
        self._listeners = []
        self._parent_chan = None
        self._accept_host = None

    async def wait_for_peer(self, uid):
        """Wait for a connection back from a spawned actor with a given
        ``uid``.
        """
        log.debug(f"Waiting for peer {uid} to connect")
        event = self._peers.setdefault(uid, trio.Event())
        await event.wait()
        return event, self._peers[uid]

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
        await chan.send(self.uid)
        uid = await chan.recv()
        chan.uid = uid
        log.info(f"Handshake with actor {uid}@{chan.raddr} complete")

        # channel tracking
        event = self._peers.pop(uid, None)
        chan.event = event
        self._peers[uid] = chan
        log.debug(f"Registered {chan} for {uid}")

        # Instructing connection: this is likely a new channel to
        # a recently spawned actor which we'd like to control via
        # async-rpc calls.
        if event and getattr(event, 'set', None):
            log.debug(f"Waking channel waiters {event.statistics()}")
            # Alert any task waiting on this connection to come up
            event.set()
            event.clear()  # now consumer can wait on this channel to close

        # Begin channel management - respond to remote requests and
        # process received reponses.
        try:
            await self._process_messages(chan)
        finally:
            # Drop ref to channel so it can be gc-ed and disconnected
            if chan is not self._parent_chan:
                log.debug(f"Releasing channel {chan}")
                self._peers.pop(chan.uid, None)
                chan.event.set()  # signal teardown/disconnection
                if not self._peers:  # no more channels connected
                    self._no_more_peers.set()
                    log.debug(f"No more peer channels")

    def _push_result(self, actorid, cid, msg):
        q = self.get_waitq(actorid, cid)
        log.debug(f"Delivering {msg} from {actorid} to caller {cid}")
        q.put_nowait(msg)

    def get_waitq(self, actorid, cid):
        if actorid not in self._actors2calls:
            log.debug(f"Registering for results from {actorid}")
        cids2qs = self._actors2calls.setdefault(actorid, {})
        if cid not in cids2qs:
            log.debug(f"Registering for result from call id {cid}")
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
        log.debug(f"Entering async-rpc loop for {chan}")
        async with trio.open_nursery() as nursery:
            async for msg in chan.aiter_recv():
                if msg is None:  # terminate sentinel
                    log.debug(f"Terminating msg loop for {chan}")
                    break
                log.debug(f"Received msg {msg}")
                cid = msg.get('cid')
                if cid:  # deliver response to local caller/waiter
                    self._push_result(chan.uid, cid, msg)
                    if 'error' in msg:
                        # TODO: need something better then this slop
                        raise RemoteActorError(msg['error'])
                    continue
                else:
                    ns, funcname, kwargs, actorid, cid = msg['cmd']

                log.debug(
                    f"Processing request from {actorid}\n"
                    f"{ns}.{funcname}({kwargs})")
                # TODO: accept a sentinel which cancels this task tree?
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

                nursery.start_soon(
                    _invoke, cid, chan, func, kwargs, treat_as_gen,
                    name=funcname
                )
        log.debug(f"Exiting msg loop for {chan}")

    def _fork_main(self, accept_addr, parent_addr=None):
        # after fork routine which invokes a fresh ``trio.run``
        log.info(
            f"Started new {ctx.current_process()} for actor {self.uid}")
        global _current_actor
        _current_actor = self
        log.debug(f"parent_addr is {parent_addr}")
        trio.run(
            partial(self._async_main, accept_addr, parent_addr=parent_addr))
        log.debug(f"Actor {self.uid} terminated")

    async def _async_main(self, accept_addr, parent_addr=None, nursery=None):
        """Start the channel server and main task.

        A "root-most" (or "top-level") nursery for this actor is opened here
        and when cancelled effectively cancels the actor.
        """
        result = None
        try:
            async with maybe_open_nursery(nursery) as nursery:
                self._root_nursery = nursery

                # Startup up channel server
                host, port = accept_addr
                await nursery.start(partial(
                    self._serve_forever, accept_host=host, accept_port=port)
                )

                if parent_addr is not None:
                    # Connect back to the parent actor and conduct initial
                    # handshake (From this point on if we error ship the
                    # exception back to the parent actor)
                    chan = self._parent_chan = Channel(
                        destaddr=parent_addr,
                        on_reconnect=self.main
                    )
                    await chan.connect()

                    # initial handshake, report who we are, who they are
                    await chan.send(self.uid)
                    uid = await chan.recv()
                    if uid in self._peers:
                        log.warn(
                            f"already have channel for {uid} registered?"
                        )

                    # handle new connection back to parent optionally
                    # begin responding to RPC
                    if self._allow_rpc:
                        self.load_namespaces()
                        nursery.start_soon(self._process_messages, chan)

                if self.main:
                    if self._parent_chan:
                        log.debug(f"Starting main task `{self.main}`")
                        # start "main" routine in a task
                        await nursery.start(
                            _invoke, 'main', self._parent_chan, self.main, {},
                            False, True  # treat_as_gen, raise_errs params
                        )
                    else:
                        # run directly
                        log.debug(f"Running `{self.main}` directly")
                        result = await self.main()

                    # terminate local in-proc once its main completes
                    log.debug(
                        f"Waiting for remaining peers {self._peers} to clear")
                    await self._no_more_peers.wait()
                    log.debug(f"All peer channels are complete")

                    # tear down channel server
                    if not self._outlive_main:
                        log.debug(f"Shutting down channel server")
                        self.cancel_server()

            # blocks here as expected if no nursery was provided until
            # the channel server is killed (i.e. this actor is
            # cancelled or signalled by the parent actor)
        except Exception:
            if self._parent_chan:
                log.exception("Actor errored:")
                await self._parent_chan.send(
                    {'error': traceback.format_exc(), 'cid': 'main'})
            else:
                raise

        return result

    async def _serve_forever(
        self,
        *,
        # (host, port) to bind for channel server
        accept_host=None,
        accept_port=0,
        task_status=trio.TASK_STATUS_IGNORED
    ):
        """Main coroutine: connect back to the parent, spawn main task, begin
        listening for new messages.

        """
        log.debug(f"Starting tcp server on {accept_host}:{accept_port}")
        async with trio.open_nursery() as nursery:
            self._server_nursery = nursery
            # TODO: might want to consider having a separate nursery
            # for the stream handler such that the server can be cancelled
            # whilst leaving existing channels up
            listeners = await nursery.start(
                partial(
                    trio.serve_tcp,
                    self._stream_handler,
                    handler_nursery=self._root_nursery,
                    port=accept_port, host=accept_host,
                )
            )
            self._listeners.extend(listeners)
            log.debug(f"Spawned {listeners}")
            task_status.started()

    def cancel(self):
        """This cancels the internal root-most nursery thereby gracefully
        cancelling (for all intents and purposes) this actor.
        """
        self._root_nursery.cancel_scope.cancel()

    def cancel_server(self):
        """Cancel the internal channel server nursery thereby
        preventing any new inbound connections from being established.
        """
        self._server_nursery.cancel_scope.cancel()

    @property
    def accept_addr(self):
        """Primary address to which the channel server is bound.
        """
        return self._listeners[0].socket.getsockname() \
            if self._listeners else None

    def get_parent(self):
        return Portal(self._parent_chan)

    def get_chan(self, actorid):
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


class Portal:
    """A 'portal' to a(n) (remote) ``Actor``.

    Allows for invoking remote routines and receiving results through an
    underlying ``tractor.Channel`` as though the remote (async)
    function / generator was invoked locally.

    Think of this like an native async IPC API.
    """
    def __init__(self, channel, event=None):
        self.channel = channel
        self._uid = channel.uid
        self._event = event

    async def __aenter__(self):
        await self.channel.connect()
        # do the handshake
        await self.channel.send(_current_actor.uid)
        self._uid = uid = await self.channel.recv()
        _current_actor._peers[uid] = self.channel
        return self

    async def aclose(self):
        await self.channel.aclose()
        if self._event:
            # alert the _stream_handler task that we are done with the channel
            # so it can terminate / move on
            self._event.set()

    async def __aexit__(self, etype, value, tb):
        await self.aclose()

    async def run(self, ns, func, **kwargs):
        """Submit a function to be scheduled and run by actor, return its
        (stream of) result(s).
        """
        # TODO: not this needs some serious work and thinking about how
        # to make async-generators the fundamental IPC API over channels!
        # (think `yield from`, `gen.send()`, and functional reactive stuff)
        chan = self.channel
        # ship a function call request to the remote actor
        actor = current_actor()

        cid, q = await actor.send_cmd(chan, ns, func, kwargs)
        # wait on first response msg
        resptype, first_msg, q = await result_from_q(q)

        if resptype == 'yield':

            async def yield_from_q():
                yield first_msg['yield']
                for msg in q:
                    try:
                        yield msg['yield']
                    except KeyError:
                        raise RemoteActorError(msg['error'])

            return yield_from_q()

        elif resptype == 'return':
            return first_msg['return']
        else:
            raise ValueError(f"Unknown msg response type: {first_msg}")


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


class ActorNursery:
    """Spawn scoped subprocess actors.
    """
    def __init__(self, actor, supervisor=None):
        self.supervisor = supervisor
        self._actor = actor
        # We'll likely want some way to cancel all sub-actors eventually
        # self.cancel_scope = cancel_scope
        self._children = {}

    async def __aenter__(self):
        return self

    async def start_actor(
        self, name,
        bind_addr=('127.0.0.1', 0),
        statespace=None,
        rpc_module_paths=None,
        main=None,
        outlive_main=False,  # sub-actors die when their main task completes
    ):
        actor = Actor(
            name,
            # modules allowed to invoked funcs from
            rpc_module_paths=rpc_module_paths,
            statespace=statespace,  # global proc state vars
            main=main,  # main coroutine to be invoked
            outlive_main=outlive_main,
        )
        parent_addr = self._actor.accept_addr
        proc = ctx.Process(
            target=actor._fork_main,
            args=(bind_addr, parent_addr),
            daemon=True,
            name=name,
        )
        proc.start()
        if not proc.is_alive():
            raise ActorFailure("Couldn't start sub-actor?")

        # wait for actor to spawn and connect back to us
        # channel should have handshake completed by the
        # local actor by the time we get a ref to it
        event, chan = await self._actor.wait_for_peer(actor.uid)
        # channel is up, get queue which delivers result from main routine
        main_q = self._actor.get_waitq(actor.uid, 'main')
        self._children[(name, proc.pid)] = (actor, proc, main_q)

        return Portal(chan, event=event)

    async def wait(self):

        async def wait_for_proc(proc):
            # TODO: timeout block here?
            if proc.is_alive():
                await trio.hazmat.wait_readable(proc.sentinel)
            # please god don't hang
            proc.join()
            log.debug(f"Joined {proc}")

        # unblocks when all waiter tasks have completed
        async with trio.open_nursery() as nursery:
            for subactor, proc, main_q in self._children.values():
                nursery.start_soon(wait_for_proc, proc)

    async def cancel(self, hard_kill=False):
        log.debug(f"Cancelling nursery")
        for subactor, proc, main_q in self._children.values():
            if proc is mp.current_process():
                # XXX: does this even make sense?
                await subactor.cancel()
            else:
                if hard_kill:
                    log.warn(f"Hard killing subactors {self._children}")
                    proc.terminate()
                    # send KeyBoardInterrupt (trio abort signal) to underlying
                    # sub-actors
                    # os.kill(proc.pid, signal.SIGINT)
                else:
                    # send cancel cmd - likely no response from subactor
                    actor = self._actor
                    cid, q = await actor.send_cmd(
                        actor.get_chan(subactor.uid),  # channel lookup
                        'self',
                        'cancel',
                        {},
                    )
        log.debug(f"Waiting on all subactors to complete")
        await self.wait()
        log.debug(f"All subactors for {self} have terminated")

    async def __aexit__(self, etype, value, tb):
        """Wait on all subactor's main routines to complete.
        """
        async def wait_for_actor(actor, proc, q):
            if proc.is_alive():
                ret_type, msg, q = await result_from_q(q)
                log.info(f"{actor.uid} main task completed with {msg}")
                if not actor._outlive_main:
                    # trigger msg loop to break
                    log.info(f"Signalling msg loop exit for {actor.uid}")
                    await self._actor.get_chan(actor.uid).send(None)

        if etype is not None:
            log.warn(f"{current_actor().uid} errored with {etype}, "
                     "cancelling nursery")
            await self.cancel()
        else:
            log.debug(f"Waiting on subactors to complete")
            async with trio.open_nursery() as nursery:
                for subactor, proc, main_q in self._children.values():
                    nursery.start_soon(wait_for_actor, subactor, proc, main_q)

        log.debug(f"Nursery teardown complete")


def current_actor() -> Actor:
    """Get the process-local actor instance.
    """
    return _current_actor


@asynccontextmanager
async def open_nursery(supervisor=None, loglevel='WARNING'):
    """Create and yield a new ``ActorNursery``.
    """
    actor = current_actor()
    if not actor:
        raise RuntimeError("No actor instance has been defined yet?")

    # TODO: figure out supervisors from erlang
    async with ActorNursery(current_actor(), supervisor) as nursery:
        yield nursery


async def serve_local_actor(actor, nursery=None, accept_addr=(None, 0)):
    """Spawn a local actor by starting a task to execute it's main
    async function.

    Blocks if no nursery is provided, in which case it is expected the nursery
    provider is responsible for waiting on the task to complete.
    """
    await actor._async_main(
        accept_addr=accept_addr,
        parent_addr=None,
        nursery=nursery,
    )
    return actor


class NoArbiterFound:
    "Couldn't find the arbiter?"


@asynccontextmanager
async def get_arbiter(host='127.0.0.1', port=1616, main=None):
    actor = current_actor()
    if actor and not actor.is_arbiter:
        try:
            # If an arbiter is already running on this host connect to it
            async with Portal(Channel((host, port))) as portal:
                yield portal
        except OSError as err:
            raise NoArbiterFound(err)
    else:
        # no arbiter found on this host so start one in-process
        arbiter = Arbiter(
            'arbiter',
            rpc_module_paths=[],  # the arbiter doesn't allow module rpc
            statespace={},  # global proc state vars
            main=main,  # main coroutine to be invoked
        )
        # assign process-local actor
        global _current_actor
        _current_actor = arbiter

        # start the arbiter in process in a new task
        async with trio.open_nursery() as nursery:

            # start local channel-server and fake the portal API
            # NOTE: this won't block since we provide the nursery
            await serve_local_actor(
                arbiter, nursery=nursery, accept_addr=(host, port))

            yield LocalPortal(arbiter)

            # XXX: If spawned locally, the arbiter is cancelled when this
            # context is complete given that there are no more active
            # peer channels connected to it.
            arbiter.cancel_server()


@asynccontextmanager
async def find_actor(name):
    """Ask the arbiter to find actor(s) by name.

    Returns a sequence of unconnected portals for each matching actor
    known to the arbiter (client code is expected to connect the portals).
    """
    async with get_arbiter() as portal:
        sockaddrs = await portal.run('self', 'find_actor', name=name)
        portals = []
        if sockaddrs:
            for sockaddr in sockaddrs:
                portals.append(Portal(Channel(sockaddr)))

        yield portals  # XXX: these are "unconnected" portals


async def _main(async_fn, args, kwargs, name):
    main = partial(async_fn, *args)
    # Creates an internal nursery which shouldn't be cancelled even if
    # the one opened below is (this is desirable because the arbitter should
    # stay up until a re-election process has taken place - which is not
    # implemented yet FYI).
    async with get_arbiter(
        host=kwargs.pop('arbiter_host', '127.0.0.1'),
        port=kwargs.pop('arbiter_port', 1616),
        main=main,
    ) as portal:
        if not current_actor().is_arbiter:
            # create a local actor and start it up its main routine
            actor = Actor(
                name or 'anonymous',
                main=main,  # main coroutine to be invoked
                **kwargs
            )
            # this will block and yield control to the `trio` run loop
            await serve_local_actor(
                actor, accept_addr=kwargs.pop('accept_addr', (None, 0)))
            log.info("Completed async main")
            # TODO: when the local actor's main has completed we cancel?
            # actor.cancel()
        else:
            # block waiting for the arbiter main task to complete
            pass


def run(async_fn, *args, arbiter_host=None, name='anonymous', **kwargs):
    """Run a trio-actor async function in process.

    This is tractor's main entry and the start point for any async actor.
    """
    return trio.run(_main, async_fn, args, kwargs, name)
