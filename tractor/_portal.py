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

'''
Memory boundary "Portals": an API for structured
concurrency linked tasks running in disparate memory domains.

'''
from __future__ import annotations
import importlib
import inspect
from typing import (
    Any, Optional,
    Callable, AsyncGenerator,
    Type,
)
from functools import partial
from dataclasses import dataclass
from pprint import pformat
import warnings

import trio
from async_generator import asynccontextmanager

from ._state import current_actor
from ._ipc import Channel
from .log import get_logger
from .msg import NamespacePath
from ._exceptions import (
    unpack_error,
    NoResult,
    ContextCancelled,
)
from ._streaming import Context, ReceiveMsgStream


log = get_logger(__name__)


@asynccontextmanager
async def maybe_open_nursery(
    nursery: trio.Nursery = None,
    shield: bool = False,
) -> AsyncGenerator[trio.Nursery, Any]:
    '''
    Create a new nursery if None provided.

    Blocks on exit as expected if no input nursery is provided.

    '''
    if nursery is not None:
        yield nursery
    else:
        async with trio.open_nursery() as nursery:
            nursery.cancel_scope.shield = shield
            yield nursery


def _unwrap_msg(

    msg: dict[str, Any],
    channel: Channel

) -> Any:
    try:
        return msg['return']
    except KeyError:
        # internal error should never get here
        assert msg.get('cid'), "Received internal error at portal?"
        raise unpack_error(msg, channel)


class MessagingError(Exception):
    'Some kind of unexpected SC messaging dialog issue'


class Portal:
    '''
    A 'portal' to a(n) (remote) ``Actor``.

    A portal is "opened" (and eventually closed) by one side of an
    inter-actor communication context. The side which opens the portal
    is equivalent to a "caller" in function parlance and usually is
    either the called actor's parent (in process tree hierarchy terms)
    or a client interested in scheduling work to be done remotely in a
    far process.

    The portal api allows the "caller" actor to invoke remote routines
    and receive results through an underlying ``tractor.Channel`` as
    though the remote (async) function / generator was called locally.
    It may be thought of loosely as an RPC api where native Python
    function calling semantics are supported transparently; hence it is
    like having a "portal" between the seperate actor memory spaces.

    '''
    # the timeout for a remote cancel request sent to
    # a(n) (peer) actor.
    cancel_timeout = 0.5

    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        # during the portal's lifetime
        self._result_msg: Optional[dict] = None

        # When set to a ``Context`` (when _submit_for_result is called)
        # it is expected that ``result()`` will be awaited at some
        # point.
        self._expect_result: Optional[Context] = None
        self._streams: set[ReceiveMsgStream] = set()
        self.actor = current_actor()

    async def _submit_for_result(
        self,
        ns: str,
        func: str,
        **kwargs
    ) -> None:

        assert self._expect_result is None, \
                "A pending main result has already been submitted"

        self._expect_result = await self.actor.start_remote_task(
            self.channel,
            ns,
            func,
            kwargs
        )

    async def _return_once(
        self,
        ctx: Context,

    ) -> dict[str, Any]:

        assert ctx._remote_func_type == 'asyncfunc'  # single response
        msg = await ctx._recv_chan.receive()
        return msg

    async def result(self) -> Any:
        '''
        Return the result(s) from the remote actor's "main" task.

        '''
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

        if self._result_msg is None:
            self._result_msg = await self._return_once(
                self._expect_result
            )

        return _unwrap_msg(self._result_msg, self.channel)

    async def _cancel_streams(self):
        # terminate all locally running async generator
        # IPC calls
        if self._streams:
            log.cancel(
                f"Cancelling all streams with {self.channel.uid}")
            for stream in self._streams.copy():
                try:
                    await stream.aclose()
                except trio.ClosedResourceError:
                    # don't error the stream having already been closed
                    # (unless of course at some point down the road we
                    # won't expect this to always be the case or need to
                    # detect it for respawning purposes?)
                    log.debug(f"{stream} was already closed.")

    async def aclose(self):
        log.debug(f"Closing {self}")
        # TODO: once we move to implementing our own `ReceiveChannel`
        # (including remote task cancellation inside its `.aclose()`)
        # we'll need to .aclose all those channels here
        await self._cancel_streams()

    async def cancel_actor(
        self,
        timeout: float = None,

    ) -> bool:
        '''
        Cancel the actor on the other end of this portal.

        '''
        if not self.channel.connected():
            log.cancel("This channel is already closed can't cancel")
            return False

        log.cancel(
            f"Sending actor cancel request to {self.channel.uid} on "
            f"{self.channel}")

        self.channel._cancel_called = True

        try:
            # send cancel cmd - might not get response
            # XXX: sure would be nice to make this work with a proper shield
            with trio.move_on_after(timeout or self.cancel_timeout) as cs:
                cs.shield = True

                await self.run_from_ns('self', 'cancel')
                return True

            if cs.cancelled_caught:
                log.cancel(f"May have failed to cancel {self.channel.uid}")

            # if we get here some weird cancellation case happened
            return False

        except (
            trio.ClosedResourceError,
            trio.BrokenResourceError,
        ):
            log.cancel(
                f"{self.channel} for {self.channel.uid} was already "
                "closed or broken?")
            return False

    async def run_from_ns(
        self,
        namespace_path: str,
        function_name: str,
        **kwargs,
    ) -> Any:
        '''
        Run a function from a (remote) namespace in a new task on the
        far-end actor.

        This is a more explitcit way to run tasks in a remote-process
        actor using explicit object-path syntax. Hint: this is how
        `.run()` works underneath.

        Note::

            A special namespace `self` can be used to invoke `Actor`
            instance methods in the remote runtime. Currently this
            should only be used solely for ``tractor`` runtime
            internals.

        '''
        ctx = await self.actor.start_remote_task(
            self.channel,
            namespace_path,
            function_name,
            kwargs,
        )
        ctx._portal = self
        msg = await self._return_once(ctx)
        return _unwrap_msg(msg, self.channel)

    async def run(
        self,
        func: str,
        fn_name: Optional[str] = None,
        **kwargs
    ) -> Any:
        '''
        Submit a remote function to be scheduled and run by actor, in
        a new task, wrap and return its (stream of) result(s).

        This is a blocking call and returns either a value from the
        remote rpc task or a local async generator instance.

        '''
        if isinstance(func, str):
            warnings.warn(
                "`Portal.run(namespace: str, funcname: str)` is now"
                "deprecated, pass a function reference directly instead\n"
                "If you still want to run a remote function by name use"
                "`Portal.run_from_ns()`",
                DeprecationWarning,
                stacklevel=2,
            )
            fn_mod_path = func
            assert isinstance(fn_name, str)

        else:  # function reference was passed directly
            if (
                not inspect.iscoroutinefunction(func) or
                (
                    inspect.iscoroutinefunction(func) and
                    getattr(func, '_tractor_stream_function', False)
                )
            ):
                raise TypeError(
                    f'{func} must be a non-streaming async function!')

            fn_mod_path, fn_name = NamespacePath.from_ref(func).to_tuple()

        ctx = await self.actor.start_remote_task(
            self.channel,
            fn_mod_path,
            fn_name,
            kwargs,
        )
        ctx._portal = self
        return _unwrap_msg(
            await self._return_once(ctx),
            self.channel,
        )

    @asynccontextmanager
    async def open_stream_from(
        self,
        async_gen_func: Callable,  # typing: ignore
        **kwargs,

    ) -> AsyncGenerator[ReceiveMsgStream, None]:

        if not inspect.isasyncgenfunction(async_gen_func):
            if not (
                inspect.iscoroutinefunction(async_gen_func) and
                getattr(async_gen_func, '_tractor_stream_function', False)
            ):
                raise TypeError(
                    f'{async_gen_func} must be an async generator function!')

        fn_mod_path, fn_name = NamespacePath.from_ref(
            async_gen_func).to_tuple()
        ctx = await self.actor.start_remote_task(
            self.channel,
            fn_mod_path,
            fn_name,
            kwargs
        )
        ctx._portal = self

        # ensure receive-only stream entrypoint
        assert ctx._remote_func_type == 'asyncgen'

        try:
            # deliver receive only stream
            async with ReceiveMsgStream(
                ctx, ctx._recv_chan,
            ) as rchan:
                self._streams.add(rchan)
                yield rchan

        finally:

            # cancel the far end task on consumer close
            # NOTE: this is a special case since we assume that if using
            # this ``.open_fream_from()`` api, the stream is one a one
            # time use and we couple the far end tasks's lifetime to
            # the consumer's scope; we don't ever send a `'stop'`
            # message right now since there shouldn't be a reason to
            # stop and restart the stream, right?
            try:
                with trio.CancelScope(shield=True):
                    await ctx.cancel()

            except trio.ClosedResourceError:
                # if the far end terminates before we send a cancel the
                # underlying transport-channel may already be closed.
                log.cancel(f'Context {ctx} was already closed?')

            # XXX: should this always be done?
            # await recv_chan.aclose()
            self._streams.remove(rchan)

    @asynccontextmanager
    async def open_context(

        self,
        func: Callable,
        **kwargs,

    ) -> AsyncGenerator[tuple[Context, Any], None]:
        '''
        Open an inter-actor task context.

        This is a synchronous API which allows for deterministic
        setup/teardown of a remote task. The yielded ``Context`` further
        allows for opening bidirectional streams, explicit cancellation
        and synchronized final result collection. See ``tractor.Context``.

        '''
        # conduct target func method structural checks
        if not inspect.iscoroutinefunction(func) and (
            getattr(func, '_tractor_contex_function', False)
        ):
            raise TypeError(
                f'{func} must be an async generator function!')

        fn_mod_path, fn_name = NamespacePath.from_ref(func).to_tuple()

        ctx = await self.actor.start_remote_task(
            self.channel,
            fn_mod_path,
            fn_name,
            kwargs
        )

        assert ctx._remote_func_type == 'context'
        msg = await ctx._recv_chan.receive()

        try:
            # the "first" value here is delivered by the callee's
            # ``Context.started()`` call.
            first = msg['started']
            ctx._started_called = True

        except KeyError:
            assert msg.get('cid'), ("Received internal error at context?")

            if msg.get('error'):
                # raise kerr from unpack_error(msg, self.channel)
                raise unpack_error(msg, self.channel) from None
            else:
                raise MessagingError(
                    f'Context for {ctx.cid} was expecting a `started` message'
                    f' but received a non-error msg:\n{pformat(msg)}'
                )

        _err: Optional[BaseException] = None
        ctx._portal = self

        uid = self.channel.uid
        cid = ctx.cid
        etype: Optional[Type[BaseException]] = None

        # deliver context instance and .started() msg value in open tuple.
        try:
            async with trio.open_nursery() as scope_nursery:
                ctx._scope_nursery = scope_nursery

                # do we need this?
                # await trio.lowlevel.checkpoint()

                yield ctx, first

        except ContextCancelled as err:
            _err = err
            if not ctx._cancel_called:
                # context was cancelled at the far end but was
                # not part of this end requesting that cancel
                # so raise for the local task to respond and handle.
                raise

            # if the context was cancelled by client code
            # then we don't need to raise since user code
            # is expecting this and the block should exit.
            else:
                log.debug(f'Context {ctx} cancelled gracefully')

        except (
            BaseException,

            # more specifically, we need to handle these but not
            # sure it's worth being pedantic:
            # Exception,
            # trio.Cancelled,
            # trio.MultiError,
            # KeyboardInterrupt,

        ) as err:
            etype = type(err)
            # the context cancels itself on any cancel
            # causing error.

            if ctx.chan.connected():
                log.cancel(
                    'Context cancelled for task, sending cancel request..\n'
                    f'task:{cid}\n'
                    f'actor:{uid}'
                )
                await ctx.cancel()
            else:
                log.warning(
                    'IPC connection for context is broken?\n'
                    f'task:{cid}\n'
                    f'actor:{uid}'
                )

            raise

        finally:
            # in the case where a runtime nursery (due to internal bug)
            # or a remote actor transmits an error we want to be
            # sure we get the error the underlying feeder mem chan.
            # if it's not raised here it *should* be raised from the
            # msg loop nursery right?
            if ctx.chan.connected():
                log.info(
                    'Waiting on final context-task result for\n'
                    f'task: {cid}\n'
                    f'actor: {uid}'
                )
                result = await ctx.result()

            # though it should be impossible for any tasks
            # operating *in* this scope to have survived
            # we tear down the runtime feeder chan last
            # to avoid premature stream clobbers.
            if ctx._recv_chan is not None:
                # should we encapsulate this in the context api?
                await ctx._recv_chan.aclose()

            if etype:
                if ctx._cancel_called:
                    log.cancel(
                        f'Context {fn_name} cancelled by caller with\n{etype}'
                    )
                elif _err is not None:
                    log.cancel(
                        f'Context for task cancelled by callee with {etype}\n'
                        f'target: `{fn_name}`\n'
                        f'task:{cid}\n'
                        f'actor:{uid}'
                    )
            else:
                log.runtime(
                    f'Context {fn_name} returned '
                    f'value from callee `{result}`'
                )

            # XXX: (MEGA IMPORTANT) if this is a root opened process we
            # wait for any immediate child in debug before popping the
            # context from the runtime msg loop otherwise inside
            # ``Actor._push_result()`` the msg will be discarded and in
            # the case where that msg is global debugger unlock (via
            # a "stop" msg for a stream), this can result in a deadlock
            # where the root is waiting on the lock to clear but the
            # child has already cleared it and clobbered IPC.
            from ._debug import maybe_wait_for_debugger
            await maybe_wait_for_debugger()

            # remove the context from runtime tracking
            self.actor._contexts.pop((self.channel.uid, ctx.cid))


@dataclass
class LocalPortal:
    '''
    A 'portal' to a local ``Actor``.

    A compatibility shim for normal portals but for invoking functions
    using an in process actor instance.

    '''
    actor: 'Actor'  # type: ignore # noqa
    channel: Channel

    async def run_from_ns(self, ns: str, func_name: str, **kwargs) -> Any:
        '''
        Run a requested local function from a namespace path and
        return it's result.

        '''
        obj = self.actor if ns == 'self' else importlib.import_module(ns)
        func = getattr(obj, func_name)
        return await func(**kwargs)


@asynccontextmanager
async def open_portal(

    channel: Channel,
    nursery: Optional[trio.Nursery] = None,
    start_msg_loop: bool = True,
    shield: bool = False,

) -> AsyncGenerator[Portal, None]:
    '''
    Open a ``Portal`` through the provided ``channel``.

    Spawns a background task to handle message processing (normally
    done by the actor-runtime implicitly).

    '''
    actor = current_actor()
    assert actor
    was_connected = False

    async with maybe_open_nursery(nursery, shield=shield) as nursery:

        if not channel.connected():
            await channel.connect()
            was_connected = True

        if channel.uid is None:
            await actor._do_handshake(channel)

        msg_loop_cs: Optional[trio.CancelScope] = None
        if start_msg_loop:
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
            await portal.aclose()

            if was_connected:
                # gracefully signal remote channel-msg loop
                await channel.send(None)
                # await channel.aclose()

            # cancel background msg loop task
            if msg_loop_cs:
                msg_loop_cs.cancel()

            nursery.cancel_scope.cancel()
