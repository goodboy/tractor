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
from .log import get_console_log


ctx = mp.get_context("forkserver")
log = get_console_log('debug')


class ActorFailure(Exception):
    "General actor failure"


# set at startup and after forks
_current_actor = None


def current_actor():
    return _current_actor


class Actor:
    """The fundamental concurrency primitive.

    An actor is the combination of a ``multiprocessing.Process``
    executing a ``trio`` task tree, communicating with other actors
    through "portals" which provide a native async API around "channels".
    """
    is_arbitter = False

    def __init__(
        self,
        name: str,
        uuid: str,
        namespaces: [str],
        main: Coroutine,
        statespace: dict,
    ):
        self.uid = (name, uuid)
        self.namespaces = namespaces
        self._mods = {}
        self.main = main
        # TODO: consider making this a dynamically defined
        # @dataclass once we get py3.7
        self.statespace = statespace

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
        else:
            # manage the channel internally
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
            async for ns, funcname, kwargs, callerid in chan.aiter_recv():
                log.debug(
                    f"Processing request from {callerid}\n"
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

    def _fork_main(self, host, parent_addr=None):
        # after fork routine which invokes a new ``trio.run``
        log.info(f"self._peers are {self._peers}")
        log.info(
            f"Started new {ctx.current_process()} for actor {self.uid}")
        global _current_actor
        _current_actor = self
        log.debug(f"parent_addr is {parent_addr}")
        trio.run(self._async_main, host, parent_addr)
        log.debug(f"Actor {self.uid} terminated")

    async def _async_main(
        self, accept_host, parent_addr, *, connect_to_parent=True,
        task_status=trio.TASK_STATUS_IGNORED
    ):
        """Main coroutine: connect back to the parent, spawn main task, begin
        listening for new messages.

        A "root-most" (or "top-level") nursery is created here and when
        cancelled effectively cancels the actor.
        """
        if accept_host is None:
            # use same host addr as parent for tcp server
            accept_host, port = parent_addr
        else:
            self.load_namespaces()
            port = 0

        async with trio.open_nursery() as nursery:
            self._root_nursery = nursery
            log.debug(f"Starting tcp server on {accept_host}:{port}")
            listeners = await nursery.start(
                partial(
                    trio.serve_tcp,
                    self._stream_handler,
                    handler_nursery=nursery,
                    port=port, host=accept_host,
                )
            )
            self._listeners.extend(listeners)
            log.debug(f"Spawned {listeners}")

            if connect_to_parent:
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
                nursery.start_soon(self._process_messages, chan)

            if self.main:
                nursery.start_soon(self.main)

            # when launched in-process, trigger awaiter's completion
            task_status.started()

    def cancel(self):
        """This cancels the internal root-most nursery thereby gracefully
        cancelling (for all intents and purposes) this actor.
        """
        self._root_nursery.cancel_scope.cancel()


class Arbiter(Actor):
    """A special actor who knows all the other actors and always has
    access to the top level nursery.

    The arbiter is by default the first actor spawned on each host
    and is responsible for keeping track of all other actors for
    coordination purposes. If a new main process is launched and an
    arbiter is already running that arbiter will be used.
    """
    _registry = defaultdict(list)
    is_arbitter = True

    def find_actors(self, name):
        return self._registry[name]

    def register_actor(self, name, sockaddr):
        self._registry[name].append(sockaddr)


class Portal:
    """A 'portal' to a(n) (remote) ``Actor``.

    Allows for invoking remote routines and receiving results through an
    underlying ``tractor.Channel`` as though the remote (async)
    function / generator was invoked locally. This of this like an async-native
    IPC API.
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
        # drop ref to channel so it can be gc-ed
        _current_actor._peers.pop(self._uid, None)
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


class ActorNursery:
    """Spawn scoped subprocess actors.
    """
    def __init__(self, supervisor=None):
        self.supervisor = supervisor
        self._parent = _current_actor
        # We'll likely want some way to cancel all sub-actors eventually
        # self.cancel_scope = cancel_scope
        self._children = {}

    async def __aenter__(self):
        return self

    async def start_actor(
        self, name, module_paths,
        host='127.0.0.1',
        statespace=None,
        main=None,
        loglevel='WARNING',
    ):
        uid = str(uuid.uuid1())
        actor = Actor(
            name,
            uid,
            module_paths,  # modules allowed to invoked funcs from
            statespace=statespace,  # global proc state vars
            main=main,  # main coroutine to be invoked
        )
        accept_addr = _current_actor._listeners[0].socket.getsockname()
        proc = ctx.Process(
            target=actor._fork_main,
            args=(host, accept_addr),
            daemon=True,
            name=name,
        )
        self._children[(name, proc.pid)] = (actor, proc)
        proc.start()

        # wait for actor to spawn and connect back to us
        # channel should have handshake completed by the
        # local actor by the time we get a ref to it
        if proc.is_alive():
            event, chan = await _current_actor.wait_for_peer(actor.uid)
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


@asynccontextmanager
async def open_nursery(supervisor=None, loglevel='WARNING'):
    """Create and yield a new ``ActorNursery``.
    """
    # TODO: figure out supervisors from erlang
    async with ActorNursery(supervisor) as nursery:
        yield nursery


@asynccontextmanager
async def get_arbiter(host='127.0.0.1', port=1616, main=None):
    try:
        async with Portal(Channel((host, port))) as portal:
            yield portal
    except OSError:
        # no arbitter found on this host so start one in-process
        uid = str(uuid.uuid1())
        arbitter = Arbiter(
            'arbiter',
            uid,
            namespaces=[],  # the arbitter doesn't allow module rpc
            statespace={},  # global proc state vars
            main=main,  # main coroutine to be invoked
        )
        global _current_actor
        _current_actor = arbitter
        async with trio.open_nursery() as nursery:
            await nursery.start(
                partial(arbitter._async_main, None,
                        (host, port), connect_to_parent=False)
            )
            async with Portal(Channel((host, port))) as portal:
                yield portal

            # the arbitter is cancelled when this context is complete
            nursery.cancel_scope.cancel()


@asynccontextmanager
async def find_actors(role):
    async with get_arbiter() as portal:
        sockaddrs = await portal.run('self', 'find_actors', name=role)
        portals = []
        if sockaddrs:
            for sockaddr in sockaddrs:
                portals.append(Portal(Channel(sockaddr)))

            yield portals  # XXX: these are "unconnected" portals
        else:
            yield None
