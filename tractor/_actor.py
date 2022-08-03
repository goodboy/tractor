# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Actor primitives and helpers

"""
from __future__ import annotations
from collections import defaultdict
from functools import partial
from itertools import chain
import importlib
import importlib.util
import inspect
import uuid
from typing import (
    Any, Optional,
    Union, TYPE_CHECKING,
    Callable,
)
from types import ModuleType
import sys
import os
from contextlib import ExitStack
import warnings

import trio  # type: ignore
from trio_typing import TaskStatus
from async_generator import aclosing

from ._ipc import Channel
from ._streaming import Context
from .log import get_logger
from ._exceptions import (
    pack_error,
    unpack_error,
    ModuleNotExposed,
    is_multi_cancelled,
    ContextCancelled,
    TransportClosed,
    StreamOverrun,
)
from . import _debug
from ._discovery import get_arbiter
from ._portal import Portal
from . import _state
from . import _mp_fixup_main


if TYPE_CHECKING:
    from ._supervise import ActorNursery


log = get_logger('tractor')


async def _invoke(

    actor: 'Actor',
    cid: str,
    chan: Channel,
    func: Callable,
    kwargs: dict[str, Any],

    is_rpc: bool = True,
    task_status: TaskStatus[
        Union[trio.CancelScope, BaseException]
    ] = trio.TASK_STATUS_IGNORED,
):
    '''
    Invoke local func and deliver result(s) over provided channel.

    This is the core "RPC task" starting machinery.

    '''
    __tracebackhide__ = True
    treat_as_gen: bool = False
    failed_resp: bool = False

    # possibly a traceback (not sure what typing is for this..)
    tb = None

    cancel_scope = trio.CancelScope()
    # activated cancel scope ref
    cs: Optional[trio.CancelScope] = None

    ctx = actor.get_context(chan, cid)
    context: bool = False

    if getattr(func, '_tractor_stream_function', False):
        # handle decorated ``@tractor.stream`` async functions
        sig = inspect.signature(func)
        params = sig.parameters

        # compat with old api
        kwargs['ctx'] = ctx

        if 'ctx' in params:
            warnings.warn(
                "`@tractor.stream decorated funcs should now declare "
                "a `stream`  arg, `ctx` is now designated for use with "
                "@tractor.context",
                DeprecationWarning,
                stacklevel=2,
            )

        elif 'stream' in params:
            assert 'stream' in params
            kwargs['stream'] = ctx

        treat_as_gen = True

    elif getattr(func, '_tractor_context_function', False):
        # handle decorated ``@tractor.context`` async function
        kwargs['ctx'] = ctx
        context = True

    # errors raised inside this block are propgated back to caller
    try:
        if not (
            inspect.isasyncgenfunction(func) or
            inspect.iscoroutinefunction(func)
        ):
            raise TypeError(f'{func} must be an async function!')

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
            with cancel_scope as cs:
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

            log.runtime(f"Finished iterating {coro}")
            # TODO: we should really support a proper
            # `StopAsyncIteration` system here for returning a final
            # value if desired
            await chan.send({'stop': True, 'cid': cid})

        # one way @stream func that gets treated like an async gen
        elif treat_as_gen:
            await chan.send({'functype': 'asyncgen', 'cid': cid})
            # XXX: the async-func may spawn further tasks which push
            # back values like an async-generator would but must
            # manualy construct the response dict-packet-responses as
            # above
            with cancel_scope as cs:
                task_status.started(cs)
                await coro

            if not cs.cancelled_caught:
                # task was not cancelled so we can instruct the
                # far end async gen to tear down
                await chan.send({'stop': True, 'cid': cid})

        elif context:
            # context func with support for bi-dir streaming
            await chan.send({'functype': 'context', 'cid': cid})

            try:
                async with trio.open_nursery() as scope_nursery:
                    ctx._scope_nursery = scope_nursery
                    cs = scope_nursery.cancel_scope
                    task_status.started(cs)
                    res = await coro
                    await chan.send({'return': res, 'cid': cid})

            except trio.MultiError:
                # if a context error was set then likely
                # thei multierror was raised due to that
                if ctx._error is not None:
                    raise ctx._error from None

                raise

            finally:
                # XXX: only pop the context tracking if
                # a ``@tractor.context`` entrypoint was called
                assert chan.uid

                # don't pop the local context until we know the
                # associated child isn't in debug any more
                await _debug.maybe_wait_for_debugger()
                ctx = actor._contexts.pop((chan.uid, cid))

                if ctx:
                    log.runtime(
                        f'Context entrypoint {func} was terminated:\n{ctx}'
                    )

            assert cs
            if cs.cancelled_caught:

                # TODO: pack in ``trio.Cancelled.__traceback__`` here
                # so they can be unwrapped and displayed on the caller
                # side!

                fname = func.__name__
                if ctx._cancel_called:
                    msg = f'{fname} cancelled itself'

                elif cs.cancel_called:
                    msg = (
                        f'{fname} was remotely cancelled by its caller '
                        f'{ctx.chan.uid}'
                    )

                # task-contex was cancelled so relay to the cancel to caller
                raise ContextCancelled(
                    msg,
                    suberror_type=trio.Cancelled,
                )

        else:
            # regular async function
            try:
                await chan.send({'functype': 'asyncfunc', 'cid': cid})
            except trio.BrokenResourceError:
                failed_resp = True
                if is_rpc:
                    raise
                else:
                    log.warning(
                        f'Failed to respond to non-rpc request: {func}'
                    )

            with cancel_scope as cs:
                task_status.started(cs)
                result = await coro
                log.cancel(f'result: {result}')
                if not failed_resp:
                    # only send result if we know IPC isn't down
                    await chan.send({'return': result, 'cid': cid})

    except (
        Exception,
        trio.MultiError
    ) as err:

        if not is_multi_cancelled(err):

            # TODO: maybe we'll want different "levels" of debugging
            # eventualy such as ('app', 'supervisory', 'runtime') ?

            # if not isinstance(err, trio.ClosedResourceError) and (
            # if not is_multi_cancelled(err) and (

            entered_debug: bool = False
            if not isinstance(err, ContextCancelled) or (
                isinstance(err, ContextCancelled) and ctx._cancel_called
            ):
                # XXX: is there any case where we'll want to debug IPC
                # disconnects as a default?
                #
                # I can't think of a reason that inspecting
                # this type of failure will be useful for respawns or
                # recovery logic - the only case is some kind of strange bug
                # in our transport layer itself? Going to keep this
                # open ended for now.

                entered_debug = await _debug._maybe_enter_pm(err)

                if not entered_debug:
                    log.exception("Actor crashed:")

        # always ship errors back to caller
        err_msg = pack_error(err, tb=tb)
        err_msg['cid'] = cid
        try:
            await chan.send(err_msg)

        # TODO: tests for this scenario:
        # - RPC caller closes connection before getting a response
        # should **not** crash this actor..
        except (
            trio.ClosedResourceError,
            trio.BrokenResourceError,
            BrokenPipeError,
        ):
            # if we can't propagate the error that's a big boo boo
            log.error(
                f"Failed to ship error to caller @ {chan.uid} !?"
            )

        if cs is None:
            # error is from above code not from rpc invocation
            task_status.started(err)

    finally:
        # RPC task bookeeping
        try:
            scope, func, is_complete = actor._rpc_tasks.pop((chan, cid))
            is_complete.set()

        except KeyError:
            if is_rpc:
                # If we're cancelled before the task returns then the
                # cancel scope will not have been inserted yet
                log.warning(
                    f"Task {func} likely errored or cancelled before start")
        finally:
            if not actor._rpc_tasks:
                log.runtime("All RPC tasks have completed")
                actor._ongoing_rpc_tasks.set()


def _get_mod_abspath(module):
    return os.path.abspath(module.__file__)


# process-global stack closed at end on actor runtime teardown
_lifetime_stack: ExitStack = ExitStack()


async def try_ship_error_to_parent(
    channel: Channel,
    err: Union[Exception, trio.MultiError],

) -> None:
    with trio.CancelScope(shield=True):
        try:
            # internal error so ship to parent without cid
            await channel.send(pack_error(err))
        except (
            trio.ClosedResourceError,
            trio.BrokenResourceError,
        ):
            # in SC terms this is one of the worst things that can
            # happen and creates the 2-general's dilemma.
            log.critical(
                f"Failed to ship error to parent "
                f"{channel.uid}, channel was closed"
            )


class Actor:
    '''
    The fundamental "runtime" concurrency primitive.

    An *actor* is the combination of a regular Python process executing
    a ``trio`` task tree, communicating with other actors through
    "memory boundary portals" - which provide a native async API around
    IPC transport "channels" which themselves encapsulate various
    (swappable) network protocols.


    Each "actor" is ``trio.run()`` scheduled "runtime" composed of many
    concurrent tasks in a single thread. The "runtime" tasks conduct
    a slew of low(er) level functions to make it possible for message
    passing between actors as well as the ability to create new actors
    (aka new "runtimes" in new processes which are supervised via
    a nursery construct). Each task which sends messages to a task in
    a "peer" (not necessarily a parent-child, depth hierarchy)) is able
    to do so via an "address", which maps IPC connections across memory
    boundaries, and task request id which allows for per-actor
    tasks to send and receive messages to specific peer-actor tasks with
    which there is an ongoing RPC/IPC dialog.

    '''
    # ugh, we need to get rid of this and replace with a "registry" sys
    # https://github.com/goodboy/tractor/issues/216
    is_arbiter: bool = False
    msg_buffer_size: int = 2**6

    # nursery placeholders filled in by `_async_main()` after fork
    _root_n: Optional[trio.Nursery] = None
    _service_n: Optional[trio.Nursery] = None
    _server_n: Optional[trio.Nursery] = None

    # Information about `__main__` from parent
    _parent_main_data: dict[str, str]
    _parent_chan_cs: Optional[trio.CancelScope] = None

    # syncs for setup/teardown sequences
    _server_down: Optional[trio.Event] = None

    # if started on ``asycio`` running ``trio`` in guest mode
    _infected_aio: bool = False

    def __init__(
        self,
        name: str,
        *,
        enable_modules: list[str] = [],
        uid: str = None,
        loglevel: str = None,
        arbiter_addr: Optional[tuple[str, int]] = None,
        spawn_method: Optional[str] = None
    ) -> None:
        """This constructor is called in the parent actor **before** the spawning
        phase (aka before a new process is executed).
        """
        self.name = name
        self.uid = (name, uid or str(uuid.uuid4()))

        self._cancel_complete = trio.Event()
        self._cancel_called: bool = False

        # retreive and store parent `__main__` data which
        # will be passed to children
        self._parent_main_data = _mp_fixup_main._mp_figure_out_main()

        # always include debugging tools module
        enable_modules.append('tractor._debug')

        mods = {}
        for name in enable_modules:
            mod = importlib.import_module(name)
            mods[name] = _get_mod_abspath(mod)

        self.enable_modules = mods
        self._mods: dict[str, ModuleType] = {}

        # TODO: consider making this a dynamically defined
        # @dataclass once we get py3.7
        self.loglevel = loglevel

        self._arb_addr = (
            str(arbiter_addr[0]),
            int(arbiter_addr[1])
        ) if arbiter_addr else None

        # marked by the process spawning backend at startup
        # will be None for the parent most process started manually
        # by the user (currently called the "arbiter")
        self._spawn_method = spawn_method

        self._peers: defaultdict = defaultdict(list)
        self._peer_connected: dict = {}
        self._no_more_peers = trio.Event()
        self._no_more_peers.set()
        self._ongoing_rpc_tasks = trio.Event()
        self._ongoing_rpc_tasks.set()

        # (chan, cid) -> (cancel_scope, func)
        self._rpc_tasks: dict[
            tuple[Channel, str],
            tuple[trio.CancelScope, Callable, trio.Event]
        ] = {}

        # map {actor uids -> Context}
        self._contexts: dict[
            tuple[tuple[str, str], str],
            Context
        ] = {}

        self._listeners: list[trio.abc.Listener] = []
        self._parent_chan: Optional[Channel] = None
        self._forkserver_info: Optional[
            tuple[Any, Any, Any, Any, Any]] = None
        self._actoruid2nursery: dict[Optional[tuple[str, str]], 'ActorNursery'] = {}  # type: ignore  # noqa

    async def wait_for_peer(
        self, uid: tuple[str, str]
    ) -> tuple[trio.Event, Channel]:
        """Wait for a connection back from a spawned actor with a given
        ``uid``.
        """
        log.runtime(f"Waiting for peer {uid} to connect")
        event = self._peer_connected.setdefault(uid, trio.Event())
        await event.wait()
        log.runtime(f"{uid} successfully connected back to us")
        return event, self._peers[uid][-1]

    def load_modules(self) -> None:
        """Load allowed RPC modules locally (after fork).

        Since this actor may be spawned on a different machine from
        the original nursery we need to try and load the local module
        code (if it exists).
        """
        try:
            if self._spawn_method == 'trio':
                parent_data = self._parent_main_data
                if 'init_main_from_name' in parent_data:
                    _mp_fixup_main._fixup_main_from_name(
                        parent_data['init_main_from_name'])
                elif 'init_main_from_path' in parent_data:
                    _mp_fixup_main._fixup_main_from_path(
                        parent_data['init_main_from_path'])

            for modpath, filepath in self.enable_modules.items():
                # XXX append the allowed module to the python path which
                # should allow for relative (at least downward) imports.
                sys.path.append(os.path.dirname(filepath))
                log.runtime(f"Attempting to import {modpath}@{filepath}")
                mod = importlib.import_module(modpath)
                self._mods[modpath] = mod
                if modpath == '__main__':
                    self._mods['__mp_main__'] = mod

        except ModuleNotFoundError:
            # it is expected the corresponding `ModuleNotExposed` error
            # will be raised later
            log.error(f"Failed to import {modpath} in {self.name}")
            raise

    def _get_rpc_func(self, ns, funcname):
        try:
            return getattr(self._mods[ns], funcname)
        except KeyError as err:
            mne = ModuleNotExposed(*err.args)

            if ns == '__main__':
                modpath = '__name__'
            else:
                modpath = f"'{ns}'"

            msg = (
                "\n\nMake sure you exposed the target module, `{ns}`, "
                "using:\n"
                "ActorNursery.start_actor(<name>, enable_modules=[{mod}])"
            ).format(
                ns=ns,
                mod=modpath,
            )

            mne.msg += msg

            raise mne

    async def _stream_handler(

        self,
        stream: trio.SocketStream,

    ) -> None:
        """Entry point for new inbound connections to the channel server.

        """
        self._no_more_peers = trio.Event()  # unset

        chan = Channel.from_stream(stream)
        uid: Optional[tuple[str, str]] = chan.uid
        log.runtime(f"New connection to us {chan}")

        # send/receive initial handshake response
        try:
            uid = await self._do_handshake(chan)

        except (
            # we need this for ``msgspec`` for some reason?
            # for now, it's been put in the stream backend.
            # trio.BrokenResourceError,
            # trio.ClosedResourceError,

            TransportClosed,
        ):
            # XXX: This may propagate up from ``Channel._aiter_recv()``
            # and ``MsgpackStream._inter_packets()`` on a read from the
            # stream particularly when the runtime is first starting up
            # inside ``open_root_actor()`` where there is a check for
            # a bound listener on the "arbiter" addr.  the reset will be
            # because the handshake was never meant took place.
            log.warning(f"Channel {chan} failed to handshake")
            return

        # channel tracking
        event = self._peer_connected.pop(uid, None)
        if event:
            # Instructing connection: this is likely a new channel to
            # a recently spawned actor which we'd like to control via
            # async-rpc calls.
            log.runtime(f"Waking channel waiters {event.statistics()}")
            # Alert any task waiting on this connection to come up
            event.set()

        chans = self._peers[uid]

        # TODO: re-use channels for new connections instead
        # of always new ones; will require changing all the
        # discovery funcs
        if chans:
            log.runtime(
                f"already have channel(s) for {uid}:{chans}?"
            )

        log.runtime(f"Registered {chan} for {uid}")  # type: ignore
        # append new channel
        self._peers[uid].append(chan)

        local_nursery: Optional[ActorNursery] = None  # noqa
        disconnected: bool = False

        # Begin channel management - respond to remote requests and
        # process received reponses.
        try:
            disconnected = await self._process_messages(chan)

        except (
            trio.Cancelled,
        ):
            log.cancel(f"Msg loop was cancelled for {chan}")
            raise

        finally:
            local_nursery = self._actoruid2nursery.get(uid, local_nursery)

            # This is set in ``Portal.cancel_actor()``. So if
            # the peer was cancelled we try to wait for them
            # to tear down their side of the connection before
            # moving on with closing our own side.
            if (
                local_nursery
            ):

                log.cancel(f"Waiting on cancel request to peer {chan.uid}")
                # XXX: this is a soft wait on the channel (and its
                # underlying transport protocol) to close from the
                # remote peer side since we presume that any channel
                # which is mapped to a sub-actor (i.e. it's managed by
                # one of our local nurseries) has a message is sent to
                # the peer likely by this actor (which is now in
                # a cancelled condition) when the local runtime here is
                # now cancelled while (presumably) in the middle of msg
                # loop processing.
                with trio.move_on_after(0.5) as cs:
                    cs.shield = True
                    # Attempt to wait for the far end to close the channel
                    # and bail after timeout (2-generals on closure).
                    assert chan.msgstream

                    log.runtime(
                        f'Draining lingering msgs from stream {chan.msgstream}'
                    )

                    async for msg in chan.msgstream.drain():
                        # try to deliver any lingering msgs
                        # before we destroy the channel.
                        # This accomplishes deterministic
                        # ``Portal.cancel_actor()`` cancellation by
                        # making sure any RPC response to that call is
                        # delivered the local calling task.
                        # TODO: factor this into a helper?
                        log.runtime(f'drained {msg} for {chan.uid}')
                        cid = msg.get('cid')
                        if cid:
                            # deliver response to local caller/waiter
                            await self._push_result(chan, cid, msg)

                    await local_nursery.exited.wait()

                if disconnected:
                    # if the transport died and this actor is still
                    # registered within a local nursery, we report that the
                    # IPC layer may have failed unexpectedly since it may be
                    # the cause of other downstream errors.
                    entry = local_nursery._children.get(uid)
                    if entry:
                        _, proc, _ = entry

                        poll = getattr(proc, 'poll', None)
                        if poll and poll() is None:
                            log.cancel(
                                f'Actor {uid} IPC broke but proc is alive?'
                            )

            # ``Channel`` teardown and closure sequence

            # Drop ref to channel so it can be gc-ed and disconnected
            log.runtime(f"Releasing channel {chan} from {chan.uid}")
            chans = self._peers.get(chan.uid)
            chans.remove(chan)

            if not chans:
                log.runtime(f"No more channels for {chan.uid}")
                self._peers.pop(uid, None)

                # for (uid, cid) in self._contexts.copy():
                #     if chan.uid == uid:
                #         self._contexts.pop((uid, cid))

            log.runtime(f"Peers is {self._peers}")

            # No more channels to other actors (at all) registered
            # as connected.
            if not self._peers:
                log.runtime("Signalling no more peer channels")
                self._no_more_peers.set()

            # XXX: is this necessary (GC should do it)?
            if chan.connected():
                # if the channel is still connected it may mean the far
                # end has not closed and we may have gotten here due to
                # an error and so we should at least try to terminate
                # the channel from this end gracefully.

                log.runtime(f"Disconnecting channel {chan}")
                try:
                    # send a msg loop terminate sentinel
                    await chan.send(None)

                    # XXX: do we want this?
                    # causes "[104] connection reset by peer" on other end
                    # await chan.aclose()

                except trio.BrokenResourceError:
                    log.runtime(f"Channel {chan.uid} was already closed")

    async def _push_result(
        self,
        chan: Channel,
        cid: str,
        msg: dict[str, Any],
    ) -> None:
        '''
        Push an RPC result to the local consumer's queue.

        '''
        uid = chan.uid
        assert uid, f"`chan.uid` can't be {uid}"
        try:
            ctx = self._contexts[(uid, cid)]
        except KeyError:
            log.warning(
                f'Ignoring msg from [no-longer/un]known context {uid}:'
                f'\n{msg}')
            return

        send_chan = ctx._send_chan

        log.runtime(f"Delivering {msg} from {chan.uid} to caller {cid}")

        # XXX: we do **not** maintain backpressure and instead
        # opt to relay stream overrun errors to the sender.
        try:
            send_chan.send_nowait(msg)
            # if an error is deteced we should always
            # expect it to be raised by any context (stream)
            # consumer task
            await ctx._maybe_raise_from_remote_msg(msg)

        except trio.BrokenResourceError:
            # TODO: what is the right way to handle the case where the
            # local task has already sent a 'stop' / StopAsyncInteration
            # to the other side but and possibly has closed the local
            # feeder mem chan? Do we wait for some kind of ack or just
            # let this fail silently and bubble up (currently)?

            # XXX: local consumer has closed their side
            # so cancel the far end streaming task
            log.warning(f"{send_chan} consumer is already closed")
            return

        except trio.WouldBlock:
            # XXX: always push an error even if the local
            # receiver is in overrun state.
            await ctx._maybe_raise_from_remote_msg(msg)

            uid = chan.uid
            lines = [
                'Task context stream was overrun',
                f'local task: {cid} @ {self.uid}',
                f'remote sender: {uid}',
            ]
            if not ctx._stream_opened:
                lines.insert(
                    1,
                    f'\n*** No stream open on `{self.uid[0]}` side! ***\n'
                )
            text = '\n'.join(lines)

            if ctx._backpressure:
                log.warning(text)
                await send_chan.send(msg)
            else:
                try:
                    raise StreamOverrun(text) from None
                except StreamOverrun as err:
                    err_msg = pack_error(err)
                    err_msg['cid'] = cid
                    try:
                        await chan.send(err_msg)
                    except trio.BrokenResourceError:
                        # XXX: local consumer has closed their side
                        # so cancel the far end streaming task
                        log.warning(f"{chan} is already closed")

    def get_context(
        self,
        chan: Channel,
        cid: str,
        msg_buffer_size: Optional[int] = None,

    ) -> Context:
        '''
        Look up or create a new inter-actor-task-IPC-linked task
        "context" which encapsulates the local task's scheduling
        enviroment including a ``trio`` cancel scope, a pair of IPC
        messaging "feeder" channels, and an RPC id unique to the
        task-as-function invocation.

        '''
        log.runtime(f"Getting result queue for {chan.uid} cid {cid}")
        actor_uid = chan.uid
        assert actor_uid
        try:
            ctx = self._contexts[(actor_uid, cid)]

            # adjust buffer size if specified
            state = ctx._send_chan._state  # type: ignore
            if msg_buffer_size and state.max_buffer_size != msg_buffer_size:
                state.max_buffer_size = msg_buffer_size

        except KeyError:
            send_chan: trio.MemorySendChannel
            recv_chan: trio.MemoryReceiveChannel
            send_chan, recv_chan = trio.open_memory_channel(
                msg_buffer_size or self.msg_buffer_size)
            ctx = Context(
                chan,
                cid,
                _send_chan=send_chan,
                _recv_chan=recv_chan,
            )
            self._contexts[(actor_uid, cid)] = ctx

        return ctx

    async def start_remote_task(
        self,
        chan: Channel,
        ns: str,
        func: str,
        kwargs: dict,
        msg_buffer_size: Optional[int] = None,

    ) -> Context:
        '''
        Send a ``'cmd'`` message to a remote actor, which starts
        a remote task-as-function entrypoint.

        Synchronously validates the endpoint type  and return a caller
        side task ``Context`` that can be used to wait for responses
        delivered by the local runtime's message processing loop.

        '''
        cid = str(uuid.uuid4())
        assert chan.uid
        ctx = self.get_context(chan, cid, msg_buffer_size=msg_buffer_size)
        log.runtime(f"Sending cmd to {chan.uid}: {ns}.{func}({kwargs})")
        await chan.send({'cmd': (ns, func, kwargs, self.uid, cid)})

        # Wait on first response msg and validate; this should be
        # immediate.
        first_msg = await ctx._recv_chan.receive()
        functype = first_msg.get('functype')

        if 'error' in first_msg:
            raise unpack_error(first_msg, chan)

        elif functype not in ('asyncfunc', 'asyncgen', 'context'):
            raise ValueError(f"{first_msg} is an invalid response packet?")

        ctx._remote_func_type = functype
        return ctx

    async def _process_messages(
        self,
        chan: Channel,
        shield: bool = False,
        task_status: TaskStatus[trio.CancelScope] = trio.TASK_STATUS_IGNORED,

    ) -> bool:
        '''
        Process messages for the channel async-RPC style.

        Receive multiplexed RPC requests and deliver responses over ``chan``.

        '''
        # TODO: once https://github.com/python-trio/trio/issues/467 gets
        # worked out we'll likely want to use that!
        msg = None
        nursery_cancelled_before_task: bool = False

        log.runtime(f"Entering msg loop for {chan} from {chan.uid}")
        try:
            with trio.CancelScope(shield=shield) as loop_cs:
                # this internal scope allows for keeping this message
                # loop running despite the current task having been
                # cancelled (eg. `open_portal()` may call this method from
                # a locally spawned task) and recieve this scope using
                # ``scope = Nursery.start()``
                task_status.started(loop_cs)
                async for msg in chan:

                    if msg is None:  # loop terminate sentinel

                        log.cancel(
                            f"Channel to {chan.uid} terminated?\n"
                            "Cancelling all associated tasks..")

                        for (channel, cid) in self._rpc_tasks.copy():
                            if channel is chan:
                                await self._cancel_task(cid, channel)

                        log.runtime(
                                f"Msg loop signalled to terminate for"
                                f" {chan} from {chan.uid}")

                        break

                    log.transport(   # type: ignore
                        f"Received msg {msg} from {chan.uid}")

                    cid = msg.get('cid')
                    if cid:
                        # deliver response to local caller/waiter
                        await self._push_result(chan, cid, msg)

                        log.runtime(
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

                    log.runtime(
                        f"Processing request from {actorid}\n"
                        f"{ns}.{funcname}({kwargs})")

                    if ns == 'self':
                        func = getattr(self, funcname)

                        if funcname == 'cancel':

                            # don't start entire actor runtime
                            # cancellation if this actor is in debug
                            # mode
                            pdb_complete = _debug.Lock.local_pdb_complete
                            if pdb_complete:
                                await pdb_complete.wait()

                            # we immediately start the runtime machinery
                            # shutdown
                            with trio.CancelScope(shield=True):
                                # self.cancel() was called so kill this
                                # msg loop and break out into
                                # ``_async_main()``
                                log.cancel(
                                    f"Actor {self.uid} was remotely cancelled "
                                    f"by {chan.uid}"
                                )
                                await _invoke(
                                    self, cid, chan, func, kwargs, is_rpc=False
                                )

                            loop_cs.cancel()
                            break

                        if funcname == '_cancel_task':

                            # we immediately start the runtime machinery
                            # shutdown
                            with trio.CancelScope(shield=True):
                                # self.cancel() was called so kill this
                                # msg loop and break out into
                                # ``_async_main()``
                                kwargs['chan'] = chan
                                log.cancel(
                                    f'Remote request to cancel task\n'
                                    f'remote actor: {chan.uid}\n'
                                    f'task: {cid}'
                                )
                                try:
                                    await _invoke(
                                        self,
                                        cid,
                                        chan,
                                        func,
                                        kwargs,
                                        is_rpc=False,
                                    )
                                except BaseException:
                                    log.exception("failed to cancel task?")

                                continue
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
                    log.runtime(f"Spawning task for {func}")
                    assert self._service_n
                    try:
                        cs = await self._service_n.start(
                            partial(_invoke, self, cid, chan, func, kwargs),
                            name=funcname,
                        )
                    except (RuntimeError, trio.MultiError):
                        # avoid reporting a benign race condition
                        # during actor runtime teardown.
                        nursery_cancelled_before_task = True
                        break

                    # never allow cancelling cancel requests (results in
                    # deadlock and other weird behaviour)
                    # if func != self.cancel:
                    if isinstance(cs, Exception):
                        log.warning(
                            f"Task for RPC func {func} failed with"
                            f"{cs}")
                    else:
                        # mark that we have ongoing rpc tasks
                        self._ongoing_rpc_tasks = trio.Event()
                        log.runtime(f"RPC func is {func}")
                        # store cancel scope such that the rpc task can be
                        # cancelled gracefully if requested
                        self._rpc_tasks[(chan, cid)] = (
                            cs, func, trio.Event())

                    log.runtime(
                        f"Waiting on next msg for {chan} from {chan.uid}")

                # end of async for, channel disconnect vis
                # ``trio.EndOfChannel``
                log.runtime(
                    f"{chan} for {chan.uid} disconnected, cancelling tasks"
                )
                await self.cancel_rpc_tasks(chan)

        except (
            TransportClosed,
        ):
            # channels "breaking" (for TCP streams by EOF or 104
            # connection-reset) is ok since we don't have a teardown
            # handshake for them (yet) and instead we simply bail out of
            # the message loop and expect the teardown sequence to clean
            # up.
            log.runtime(f'channel from {chan.uid} closed abruptly:\n{chan}')

            # transport **was** disconnected
            return True

        except (Exception, trio.MultiError) as err:
            if nursery_cancelled_before_task:
                sn = self._service_n
                assert sn and sn.cancel_scope.cancel_called
                log.cancel(
                    f'Service nursery cancelled before it handled {funcname}'
                )
            else:
                # ship any "internal" exception (i.e. one from internal
                # machinery not from an rpc task) to parent
                log.exception("Actor errored:")
                if self._parent_chan:
                    await try_ship_error_to_parent(self._parent_chan, err)

            # if this is the `MainProcess` we expect the error broadcasting
            # above to trigger an error at consuming portal "checkpoints"
            raise

        finally:
            # msg debugging for when he machinery is brokey
            log.runtime(
                f"Exiting msg loop for {chan} from {chan.uid} "
                f"with last msg:\n{msg}")

        # transport **was not** disconnected
        return False

    async def _from_parent(
        self,
        parent_addr: Optional[tuple[str, int]],
    ) -> tuple[Channel, Optional[tuple[str, int]]]:
        try:
            # Connect back to the parent actor and conduct initial
            # handshake. From this point on if we error, we
            # attempt to ship the exception back to the parent.
            chan = Channel(
                destaddr=parent_addr,
            )
            await chan.connect()

            # Initial handshake: swap names.
            await self._do_handshake(chan)

            accept_addr: Optional[tuple[str, int]] = None

            if self._spawn_method == "trio":
                # Receive runtime state from our parent
                parent_data: dict[str, Any]
                parent_data = await chan.recv()
                log.runtime(
                    "Received state from parent:\n"
                    f"{parent_data}"
                )
                accept_addr = (
                    parent_data.pop('bind_host'),
                    parent_data.pop('bind_port'),
                )
                rvs = parent_data.pop('_runtime_vars')
                log.runtime(f"Runtime vars are: {rvs}")
                rvs['_is_root'] = False
                _state._runtime_vars.update(rvs)

                for attr, value in parent_data.items():

                    if attr == '_arb_addr':
                        # XXX: ``msgspec`` doesn't support serializing tuples
                        # so just cash manually here since it's what our
                        # internals expect.
                        value = tuple(value) if value else None
                        self._arb_addr = value

                    else:
                        setattr(self, attr, value)

            return chan, accept_addr

        except OSError:  # failed to connect
            log.warning(
                f"Failed to connect to parent @ {parent_addr},"
                " closing server")
            await self.cancel()
            raise

    async def _async_main(
        self,
        accept_addr: Optional[tuple[str, int]] = None,

        # XXX: currently ``parent_addr`` is only needed for the
        # ``multiprocessing`` backend (which pickles state sent to
        # the child instead of relaying it over the connect-back
        # channel). Once that backend is removed we can likely just
        # change this to a simple ``is_subactor: bool`` which will
        # be False when running as root actor and True when as
        # a subactor.
        parent_addr: Optional[tuple[str, int]] = None,
        task_status: TaskStatus[None] = trio.TASK_STATUS_IGNORED,

    ) -> None:
        """
        Start the channel server, maybe connect back to the parent, and
        start the main task.

        A "root-most" (or "top-level") nursery for this actor is opened here
        and when cancelled effectively cancels the actor.

        """
        registered_with_arbiter = False
        try:

            # establish primary connection with immediate parent
            self._parent_chan = None
            if parent_addr is not None:

                self._parent_chan, accept_addr_rent = await self._from_parent(
                    parent_addr)

                # either it's passed in because we're not a child
                # or because we're running in mp mode
                if accept_addr_rent is not None:
                    accept_addr = accept_addr_rent

            # load exposed/allowed RPC modules
            # XXX: do this **after** establishing a channel to the parent
            # but **before** starting the message loop for that channel
            # such that import errors are properly propagated upwards
            self.load_modules()

            # The "root" nursery ensures the channel with the immediate
            # parent is kept alive as a resilient service until
            # cancellation steps have (mostly) occurred in
            # a deterministic way.
            async with trio.open_nursery() as root_nursery:
                self._root_n = root_nursery
                assert self._root_n

                async with trio.open_nursery() as service_nursery:
                    # This nursery is used to handle all inbound
                    # connections to us such that if the TCP server
                    # is killed, connections can continue to process
                    # in the background until this nursery is cancelled.
                    self._service_n = service_nursery
                    assert self._service_n

                    # Startup up the channel server with,
                    # - subactor: the bind address is sent by our parent
                    #   over our established channel
                    # - root actor: the ``accept_addr`` passed to this method
                    assert accept_addr
                    host, port = accept_addr

                    self._server_n = await service_nursery.start(
                        partial(
                            self._serve_forever,
                            service_nursery,
                            accept_host=host,
                            accept_port=port
                        )
                    )
                    accept_addr = self.accept_addr
                    if _state._runtime_vars['_is_root']:
                        _state._runtime_vars['_root_mailbox'] = accept_addr

                    # Register with the arbiter if we're told its addr
                    log.runtime(f"Registering {self} for role `{self.name}`")
                    assert isinstance(self._arb_addr, tuple)

                    async with get_arbiter(*self._arb_addr) as arb_portal:
                        await arb_portal.run_from_ns(
                            'self',
                            'register_actor',
                            uid=self.uid,
                            sockaddr=accept_addr,
                        )

                    registered_with_arbiter = True

                    # init steps complete
                    task_status.started()

                    # Begin handling our new connection back to our
                    # parent. This is done last since we don't want to
                    # start processing parent requests until our channel
                    # server is 100% up and running.
                    if self._parent_chan:
                        await root_nursery.start(
                            partial(
                                self._process_messages,
                                self._parent_chan,
                                shield=True,
                            )
                        )
                    log.runtime("Waiting on service nursery to complete")
                log.runtime("Service nursery complete")
                log.runtime("Waiting on root nursery to complete")

            # Blocks here as expected until the root nursery is
            # killed (i.e. this actor is cancelled or signalled by the parent)
        except Exception as err:
            log.info("Closing all actor lifetime contexts")
            _lifetime_stack.close()

            if not registered_with_arbiter:
                # TODO: I guess we could try to connect back
                # to the parent through a channel and engage a debugger
                # once we have that all working with std streams locking?
                log.exception(
                    f"Actor errored and failed to register with arbiter "
                    f"@ {self._arb_addr}?")
                log.error(
                    "\n\n\t^^^ THIS IS PROBABLY A TRACTOR BUGGGGG!!! ^^^\n"
                    "\tCALMLY CALL THE AUTHORITIES AND HIDE YOUR CHILDREN.\n\n"
                    "\tYOUR PARENT CODE IS GOING TO KEEP WORKING FINE!!!\n"
                    "\tTHIS IS HOW RELIABlE SYSTEMS ARE SUPPOSED TO WORK!?!?\n"
                )

            if self._parent_chan:
                await try_ship_error_to_parent(self._parent_chan, err)

            # always!
            log.exception("Actor errored:")
            raise

        finally:
            log.info("Runtime nursery complete")

            # tear down all lifetime contexts if not in guest mode
            # XXX: should this just be in the entrypoint?
            log.info("Closing all actor lifetime contexts")

            # TODO: we can't actually do this bc the debugger
            # uses the _service_n to spawn the lock task, BUT,
            # in theory if we had the root nursery surround this finally
            # block it might be actually possible to debug THIS
            # machinery in the same way as user task code?
            # if self.name == 'brokerd.ib':
            #     with trio.CancelScope(shield=True):
            #         await _debug.breakpoint()

            _lifetime_stack.close()

            # Unregister actor from the arbiter
            if registered_with_arbiter and (
                    self._arb_addr is not None
            ):
                failed = False
                with trio.move_on_after(0.5) as cs:
                    cs.shield = True
                    try:
                        async with get_arbiter(*self._arb_addr) as arb_portal:
                            await arb_portal.run_from_ns(
                                'self',
                                'unregister_actor',
                                uid=self.uid
                            )
                    except OSError:
                        failed = True
                if cs.cancelled_caught:
                    failed = True
                if failed:
                    log.warning(
                        f"Failed to unregister {self.name} from arbiter")

            # Ensure all peers (actors connected to us as clients) are finished
            if not self._no_more_peers.is_set():
                if any(
                    chan.connected() for chan in chain(*self._peers.values())
                ):
                    log.runtime(
                        f"Waiting for remaining peers {self._peers} to clear")
                    with trio.CancelScope(shield=True):
                        await self._no_more_peers.wait()
            log.runtime("All peer channels are complete")

        log.runtime("Runtime completed")

    async def _serve_forever(
        self,
        handler_nursery: trio.Nursery,
        *,
        # (host, port) to bind for channel server
        accept_host: tuple[str, int] = None,
        accept_port: int = 0,
        task_status: TaskStatus[trio.Nursery] = trio.TASK_STATUS_IGNORED,
    ) -> None:
        """Start the channel server, begin listening for new connections.

        This will cause an actor to continue living (blocking) until
        ``cancel_server()`` is called.
        """
        self._server_down = trio.Event()
        try:
            async with trio.open_nursery() as server_n:
                l: list[trio.abc.Listener] = await server_n.start(
                    partial(
                        trio.serve_tcp,
                        self._stream_handler,
                        # new connections will stay alive even if this server
                        # is cancelled
                        handler_nursery=handler_nursery,
                        port=accept_port,
                        host=accept_host,
                    )
                )
                log.runtime(
                    "Started tcp server(s) on"
                    f" {[getattr(l, 'socket', 'unknown socket') for l in l]}")
                self._listeners.extend(l)
                task_status.started(server_n)
        finally:
            # signal the server is down since nursery above terminated
            self._server_down.set()

    def cancel_soon(self) -> None:
        """Cancel this actor asap; can be called from a sync context.

        Schedules `.cancel()` to be run immediately just like when
        cancelled by the parent.
        """
        assert self._service_n
        self._service_n.start_soon(self.cancel)

    async def cancel(self) -> bool:
        """Cancel this actor's runtime.

        The "deterministic" teardown sequence in order is:
            - cancel all ongoing rpc tasks by cancel scope
            - cancel the channel server to prevent new inbound
              connections
            - cancel the "service" nursery reponsible for
              spawning new rpc tasks
            - return control the parent channel message loop
        """
        log.cancel(f"{self.uid} is trying to cancel")
        self._cancel_called = True

        # cancel all ongoing rpc tasks
        with trio.CancelScope(shield=True):

            # kill any debugger request task to avoid deadlock
            # with the root actor in this tree
            dbcs = _debug.Lock._debugger_request_cs
            if dbcs is not None:
                log.cancel("Cancelling active debugger request")
                dbcs.cancel()

            # kill all ongoing tasks
            await self.cancel_rpc_tasks()

            # stop channel server
            self.cancel_server()
            if self._server_down is not None:
                await self._server_down.wait()
            else:
                log.warning(
                    f'{self.uid} was likely cancelled before it started')

            # cancel all rpc tasks permanently
            if self._service_n:
                self._service_n.cancel_scope.cancel()

        log.cancel(f"{self.uid} called `Actor.cancel()`")
        self._cancel_complete.set()
        return True

    # XXX: hard kill logic if needed?
    # def _hard_mofo_kill(self):
    #     # If we're the root actor or zombied kill everything
    #     if self._parent_chan is None:  # TODO: more robust check
    #         root = trio.lowlevel.current_root_task()
    #         for n in root.child_nurseries:
    #             n.cancel_scope.cancel()

    async def _cancel_task(self, cid, chan):
        '''
        Cancel a local task by call-id / channel.

        Note this method will be treated as a streaming function
        by remote actor-callers due to the declaration of ``ctx``
        in the signature (for now).

        '''
        # right now this is only implicitly called by
        # streaming IPC but it should be called
        # to cancel any remotely spawned task
        try:
            # this ctx based lookup ensures the requested task to
            # be cancelled was indeed spawned by a request from this channel
            scope, func, is_complete = self._rpc_tasks[(chan, cid)]
        except KeyError:
            log.cancel(f"{cid} has already completed/terminated?")
            return

        log.cancel(
            f"Cancelling task:\ncid: {cid}\nfunc: {func}\n"
            f"peer: {chan.uid}\n")

        # don't allow cancelling this function mid-execution
        # (is this necessary?)
        if func is self._cancel_task:
            return

        scope.cancel()

        # wait for _invoke to mark the task complete
        log.runtime(
            f"Waiting on task to cancel:\ncid: {cid}\nfunc: {func}\n"
            f"peer: {chan.uid}\n")
        await is_complete.wait()

        log.runtime(
            f"Sucessfully cancelled task:\ncid: {cid}\nfunc: {func}\n"
            f"peer: {chan.uid}\n")

    async def cancel_rpc_tasks(
        self,
        only_chan: Optional[Channel] = None,
    ) -> None:
        """Cancel all existing RPC responder tasks using the cancel scope
        registered for each.
        """
        tasks = self._rpc_tasks
        if tasks:
            log.cancel(f"Cancelling all {len(tasks)} rpc tasks:\n{tasks} ")
            for (
                (chan, cid),
                (scope, func, is_complete),
            ) in tasks.copy().items():
                if only_chan is not None:
                    if only_chan != chan:
                        continue

                # TODO: this should really done in a nursery batch
                if func != self._cancel_task:
                    await self._cancel_task(cid, chan)

            log.cancel(
                f"Waiting for remaining rpc tasks to complete {tasks}")
            await self._ongoing_rpc_tasks.wait()

    def cancel_server(self) -> None:
        """Cancel the internal channel server nursery thereby
        preventing any new inbound connections from being established.
        """
        if self._server_n:
            log.runtime("Shutting down channel server")
            self._server_n.cancel_scope.cancel()

    @property
    def accept_addr(self) -> Optional[tuple[str, int]]:
        """Primary address to which the channel server is bound.
        """
        # throws OSError on failure
        return self._listeners[0].socket.getsockname()  # type: ignore

    def get_parent(self) -> Portal:
        """Return a portal to our parent actor."""
        assert self._parent_chan, "No parent channel for this actor?"
        return Portal(self._parent_chan)

    def get_chans(self, uid: tuple[str, str]) -> list[Channel]:
        """Return all channels to the actor with provided uid."""
        return self._peers[uid]

    async def _do_handshake(
        self,
        chan: Channel

    ) -> tuple[str, str]:
        """Exchange (name, UUIDs) identifiers as the first communication step.

        These are essentially the "mailbox addresses" found in actor model
        parlance.
        """
        await chan.send(self.uid)
        value = await chan.recv()
        uid: tuple[str, str] = (str(value[0]), str(value[1]))

        if not isinstance(uid, tuple):
            raise ValueError(f"{uid} is not a valid uid?!")

        chan.uid = str(uid[0]), str(uid[1])
        log.runtime(f"Handshake with actor {uid}@{chan.raddr} complete")
        return uid

    def is_infected_aio(self) -> bool:
        return self._infected_aio


class Arbiter(Actor):
    '''
    A special actor who knows all the other actors and always has
    access to a top level nursery.

    The arbiter is by default the first actor spawned on each host
    and is responsible for keeping track of all other actors for
    coordination purposes. If a new main process is launched and an
    arbiter is already running that arbiter will be used.

    '''
    is_arbiter = True

    def __init__(self, *args, **kwargs):

        self._registry: dict[
            tuple[str, str],
            tuple[str, int],
        ] = {}
        self._waiters = {}

        super().__init__(*args, **kwargs)

    async def find_actor(self, name: str) -> Optional[tuple[str, int]]:
        for uid, sockaddr in self._registry.items():
            if name in uid:
                return sockaddr

        return None

    async def get_registry(
        self

    ) -> dict[str, tuple[str, int]]:
        '''
        Return current name registry.

        This method is async to allow for cross-actor invocation.

        '''
        # NOTE: requires ``strict_map_key=False`` to the msgpack
        # unpacker since we have tuples as keys (not this makes the
        # arbiter suscetible to hashdos):
        # https://github.com/msgpack/msgpack-python#major-breaking-changes-in-msgpack-10
        return {'.'.join(key): val for key, val in self._registry.items()}

    async def wait_for_actor(
        self,
        name: str,

    ) -> list[tuple[str, int]]:
        '''
        Wait for a particular actor to register.

        This is a blocking call if no actor by the provided name is currently
        registered.

        '''
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

    async def register_actor(
        self,
        uid: tuple[str, str],
        sockaddr: tuple[str, int]

    ) -> None:
        uid = name, uuid = (str(uid[0]), str(uid[1]))
        self._registry[uid] = (str(sockaddr[0]), int(sockaddr[1]))

        # pop and signal all waiter events
        events = self._waiters.pop(name, ())
        self._waiters.setdefault(name, []).append(uid)
        for event in events:
            if isinstance(event, trio.Event):
                event.set()

    async def unregister_actor(
        self,
        uid: tuple[str, str]

    ) -> None:
        uid = (str(uid[0]), str(uid[1]))
        self._registry.pop(uid)
