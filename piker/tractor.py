"""
tracor: An actor model micro-framework.
"""
import uuid
import inspect
import importlib
from functools import partial
import multiprocessing as mp
from typing import Coroutine
from collections import defaultdict

import trio
from async_generator import asynccontextmanager

from .ipc import Channel
from .log import get_console_log, get_logger


ctx = mp.get_context("forkserver")
log = get_logger('tractor')

# for debugging
log = get_console_log('debug')


class ActorFailure(Exception):
    "General actor failure"


# set at startup and after forks
_current_actor = None


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
        namespaces: [str],
        main: Coroutine,
        statespace: dict,
        uid: str = None,
        allow_rpc: bool = True,
    ):
        self.uid = (name, uid or str(uuid.uuid1()))
        self.namespaces = namespaces
        self._mods = {}
        self.main = main
        # TODO: consider making this a dynamically defined
        # @dataclass once we get py3.7
        self.statespace = statespace
        self._allow_rpc = allow_rpc

        # filled in by `_async_main` after fork
        self._peers = {}
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
        for path in self.namespaces:
            self._mods[path] = importlib.import_module(path)

    async def _stream_handler(
        self,
        stream: trio.SocketStream,
    ):
        """Receive requests and deliver responses spinning up new
        channels where necessary.

        Basically RPC with an async twist ;)
        """
        chan = Channel(stream=stream)
        log.info(f"New {chan} connected to us")
        # send/receive initial handshake response
        await chan.send(self.uid)
        uid = await chan.recv()
        log.info(f"Handshake with actor {uid}@{chan.raddr} complete")

        # XXX WTF!?!! THIS BLOCKS RANDOMLY?
        # assert tuple(raddr) == chan.laddr

        # execute main coroutine provided by spawner
        if self.main:
            await self.main(actor=self)

        event = self._peers.pop(uid, None)
        self._peers[uid] = chan
        log.info(f"Registered {chan} for {uid}")
        log.debug(f"Retrieved event {event}")

        # Instructing connection: this is likely a new channel to
        # a recently spawned actor which we'd like to control via
        # async-rpc calls.
        if event and getattr(event, 'set', None):

            log.info(f"Waking waiters of {event.statistics()}")
            # Alert any task waiting on this connection to come up
            # and don't manage channel messages as some external task is
            # waiting to use the channel
            # (usually an actor nursery)
            event.set()
            event.clear()

            # wait for channel consumer (usually a portal) to be
            # done with the channel
            await event.wait()

            # Drop ref to channel so it can be gc-ed
            self._peers.pop(self._uid, None)

        # Remote controlled connection, we are likely a subactor
        # being told what to do so manage the channel with async-rpc
        else:
            await self._process_messages(chan)

    async def _process_messages(self, chan, treat_as_gen=False):
        """Process inbound messages async-RPC style.
        """
        async def invoke(func, kwargs):
            if not inspect.iscoroutinefunction(func):
                await chan.send('func')
                await chan.send(func(**kwargs))
            else:
                coro = func(**kwargs)

                if inspect.isasyncgen(coro):
                    await chan.send('gen')
                    async for item in coro:
                        # TODO: can we send values back in here?
                        # How do we do it, spawn another task?
                        # to_send = await chan.recv()
                        # if to_send is not None:
                        #     await coro.send(to_send)
                        await chan.send(item)
                else:
                    if treat_as_gen:
                        await chan.send('gen')
                    else:
                        await chan.send('func')

                    # XXX: the async-func may spawn further tasks which push
                    # back values like an async-generator would
                    await chan.send(await coro)

        log.debug(f"Entering async-rpc loop for {chan.laddr}->{chan.raddr}")
        async with trio.open_nursery() as nursery:
            async for ns, funcname, kwargs, actorid in chan.aiter_recv():
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
                    kwargs['chan'] = chan
                    # TODO: eventually we want to be more stringent
                    # about what is considered a far-end async-generator.
                    # Right now both actual async gens and any async
                    # function which declares a `chan` kwarg in its
                    # signature will be treated as one.
                    treat_as_gen = True

                nursery.start_soon(invoke, func, kwargs, name=funcname)

    def _fork_main(self, accept_addr, parent_addr=None):
        # after fork routine which invokes a fresh ``trio.run``
        log.info(f"self._peers are {self._peers}")
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
        async with maybe_open_nursery(nursery) as nursery:
            self._root_nursery = nursery

            # Startup up channel server, optionally begin serving RPC
            # requests from the parent.
            host, port = accept_addr
            await self._serve_forever(
                nursery, accept_host=host, accept_port=port,
                parent_addr=parent_addr
            )

            # start "main" routine in a task
            if self.main:
                await self.main(self)

            # blocks here as expected if no nursery was provided until
            # the channel server is killed

    async def _serve_forever(
        self,
        nursery,  # spawns main func and channel server
        *,
        # (host, port) to bind for channel server
        accept_host=None,
        accept_port=0,
        parent_addr=None,
        task_status=trio.TASK_STATUS_IGNORED
    ):
        """Main coroutine: connect back to the parent, spawn main task, begin
        listening for new messages.

        """
        log.debug(f"Starting tcp server on {accept_host}:{accept_port}")
        listeners = await nursery.start(
            partial(
                trio.serve_tcp,
                self._stream_handler,
                handler_nursery=nursery,
                port=accept_port, host=accept_host,
            )
        )
        self._listeners.extend(listeners)
        log.debug(f"Spawned {listeners}")

        if parent_addr is not None:
            # Connect back to the parent actor and conduct initial
            # handshake (From this point on if we error ship the
            # exception back to the parent actor)
            chan = self._parent_chan = Channel(
                destaddr=parent_addr,
                on_reconnect=self.main
            )
            await chan.connect()

            # initial handshake, report who we are, figure out who they are
            await chan.send(self.uid)
            uid = await chan.recv()
            if uid in self._peers:
                log.warn(
                    f"already have channel for {uid} registered?"
                )
            else:
                self._peers[uid] = chan

            # handle new connection back to parent
            if self._allow_rpc:
                self.load_namespaces()
                nursery.start_soon(self._process_messages, chan)

        # when launched in-process, trigger awaiter's completion
        task_status.started()

    def cancel(self):
        """This cancels the internal root-most nursery thereby gracefully
        cancelling (for all intents and purposes) this actor.
        """
        self._root_nursery.cancel_scope.cancel()

    @property
    def accept_addr(self):
        """Primary address to which the channel server is bound.
        """
        return self._listeners[0].socket.getsockname() \
            if self._listeners else None

    def get_parent(self):
        return Portal(self._parent_chan)


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
        self._uid = None
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
        await chan.send((ns, func, kwargs, _current_actor.uid))
        # get expected response type
        functype = await chan.recv()
        if functype == 'gen':
            return chan.aiter_recv()
        else:
            return await chan.recv()


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
    def __init__(self, parent_actor, supervisor=None):
        self.supervisor = supervisor
        self._parent_actor = parent_actor
        # We'll likely want some way to cancel all sub-actors eventually
        # self.cancel_scope = cancel_scope
        self._children = {}

    async def __aenter__(self):
        return self

    async def start_actor(
        self, name, module_paths,
        bind_addr=('127.0.0.1', 0),
        statespace=None,
        main=None,
    ):
        actor = Actor(
            name,
            module_paths,  # modules allowed to invoked funcs from
            statespace=statespace,  # global proc state vars
            main=main,  # main coroutine to be invoked
        )
        parent_addr = self._parent_actor.accept_addr
        proc = ctx.Process(
            target=actor._fork_main,
            args=(bind_addr, parent_addr),
            daemon=True,
            name=name,
        )
        self._children[(name, proc.pid)] = (actor, proc)
        proc.start()

        # wait for actor to spawn and connect back to us
        # channel should have handshake completed by the
        # local actor by the time we get a ref to it
        if proc.is_alive():
            event, chan = await self._parent_actor.wait_for_peer(actor.uid)
        else:
            raise ActorFailure("Couldn't start sub-actor?")

        return Portal(chan)

    async def cancel(self):
        async def wait_for_proc(proc):
            # TODO: timeout block here?
            if proc.is_alive():
                await trio.hazmat.wait_readable(proc.sentinel)
            # please god don't hang
            proc.join()
            log.debug(f"Joined {proc}")

        # unblocks when all waiter tasks have completed
        async with trio.open_nursery() as nursery:
            for actor, proc in self._children.values():
                if proc is mp.current_process():
                    actor.cancel()
                else:
                    # send KeyBoardInterrupt (trio abort signal) to underlying
                    # sub-actors
                    proc.terminate()
                    # os.kill(proc.pid, signal.SIGINT)
                    nursery.start_soon(wait_for_proc, proc)

    async def __aexit__(self, etype, value, tb):
        await self.cancel()


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
            namespaces=[],  # the arbiter doesn't allow module rpc
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

            # If spawned locally, the arbiter is cancelled when this context
            # is complete (i.e the underlying context manager block completes)
            nursery.cancel_scope.cancel()


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
    # Creates an internal nursery which shouldn't be cancelled even if
    # the one opened below is (this is desirable because the arbitter should
    # stay up until a re-election process has taken place - which is not
    # implemented yet FYI).
    async with get_arbiter(
        host=kwargs.get('arbiter_host', '127.0.0.1'),
        port=kwargs.get('arbiter_port', 1616),
        main=partial(async_fn, *args, **kwargs)
    ) as portal:
        if not current_actor().is_arbiter:
            # create a local actor and start it up its main routine
            actor = Actor(
                name or 'anonymous',
                # namespaces=kwargs.get('namespaces'),
                # statespace=kwargs.get('statespace'),
                # main=async_fn,  # main coroutine to be invoked
                **kwargs
            )
            # this will block and yield control to the `trio` run loop
            await serve_local_actor(
                actor, accept_addr=kwargs.get('accept_addr', (None, 0)))
            log.info("Completed async main")
        else:
            # block waiting for the arbiter main task to complete
            pass


def run(async_fn, *args, arbiter_host=None, name='anonymous', **kwargs):
    """Run a trio-actor async function in process.

    This is tractor's main entry and the start point for any async actor.
    """
    return trio.run(_main, async_fn, args, kwargs, name)
