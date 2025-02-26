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
Memory "portal" contruct.

"Memory portals" are both an API and set of IPC wrapping primitives
for managing structured concurrency "cancel-scope linked" tasks
running in disparate virtual memory domains - at least in different
OS processes, possibly on different (hardware) hosts.

'''
from __future__ import annotations
from contextlib import asynccontextmanager as acm
import importlib
import inspect
from typing import (
    Any,
    Callable,
    AsyncGenerator,
    TYPE_CHECKING,
)
from functools import partial
from dataclasses import dataclass
import warnings

import trio

from .trionics import maybe_open_nursery
from ._state import (
    current_actor,
)
from ._ipc import Channel
from .log import get_logger
from .msg import (
    # Error,
    PayloadMsg,
    NamespacePath,
    Return,
)
from ._exceptions import (
    # unpack_error,
    NoResult,
)
from ._context import (
    Context,
    open_context_from_portal,
)
from ._streaming import (
    MsgStream,
)

if TYPE_CHECKING:
    from ._runtime import Actor

log = get_logger(__name__)


class Portal:
    '''
    A 'portal' to a memory-domain-separated `Actor`.

    A portal is "opened" (and eventually closed) by one side of an
    inter-actor communication context. The side which opens the portal
    is equivalent to a "caller" in function parlance and usually is
    either the called actor's parent (in process tree hierarchy terms)
    or a client interested in scheduling work to be done remotely in a
    process which has a separate (virtual) memory domain.

    The portal api allows the "caller" actor to invoke remote routines
    and receive results through an underlying ``tractor.Channel`` as
    though the remote (async) function / generator was called locally.
    It may be thought of loosely as an RPC api where native Python
    function calling semantics are supported transparently; hence it is
    like having a "portal" between the seperate actor memory spaces.

    '''
    # global timeout for remote cancel requests sent to
    # connected (peer) actors.
    cancel_timeout: float = 0.5

    def __init__(
        self,
        channel: Channel,
    ) -> None:

        self._chan: Channel = channel
        # during the portal's lifetime
        self._final_result_pld: Any|None = None
        self._final_result_msg: PayloadMsg|None = None

        # When set to a ``Context`` (when _submit_for_result is called)
        # it is expected that ``result()`` will be awaited at some
        # point.
        self._expect_result_ctx: Context|None = None
        self._streams: set[MsgStream] = set()
        self.actor: Actor = current_actor()

    @property
    def chan(self) -> Channel:
        return self._chan

    @property
    def channel(self) -> Channel:
        '''
        Proxy to legacy attr name..

        Consider the shorter `Portal.chan` instead of `.channel` ;)
        '''
        log.debug(
            'Consider the shorter `Portal.chan` instead of `.channel` ;)'
        )
        return self.chan

    # TODO: factor this out into a `.highlevel` API-wrapper that uses
    # a single `.open_context()` call underneath.
    async def _submit_for_result(
        self,
        ns: str,
        func: str,
        **kwargs
    ) -> None:

        if self._expect_result_ctx is not None:
            raise RuntimeError(
                'A pending main result has already been submitted'
            )

        self._expect_result_ctx: Context = await self.actor.start_remote_task(
            self.channel,
            nsf=NamespacePath(f'{ns}:{func}'),
            kwargs=kwargs,
            portal=self,
        )

    # TODO: we should deprecate this API right? since if we remove
    # `.run_in_actor()` (and instead move it to a `.highlevel`
    # wrapper api (around a single `.open_context()` call) we don't
    # really have any notion of a "main" remote task any more?
    #
    # @api_frame
    async def wait_for_result(
        self,
        hide_tb: bool = True,
    ) -> Any:
        '''
        Return the final result delivered by a `Return`-msg from the
        remote peer actor's "main" task's `return` statement.

        '''
        __tracebackhide__: bool = hide_tb
        # Check for non-rpc errors slapped on the
        # channel for which we always raise
        exc = self.channel._exc
        if exc:
            raise exc

        # not expecting a "main" result
        if self._expect_result_ctx is None:
            log.warning(
                f"Portal for {self.channel.uid} not expecting a final"
                " result?\nresult() should only be called if subactor"
                " was spawned with `ActorNursery.run_in_actor()`")
            return NoResult

        # expecting a "main" result
        assert self._expect_result_ctx

        if self._final_result_msg is None:
            try:
                (
                    self._final_result_msg,
                    self._final_result_pld,
                ) = await self._expect_result_ctx._pld_rx.recv_msg_w_pld(
                    ipc=self._expect_result_ctx,
                    expect_msg=Return,
                )
            except BaseException as err:
                # TODO: wrap this into `@api_frame` optionally with
                # some kinda filtering mechanism like log levels?
                __tracebackhide__: bool = False
                raise err

        return self._final_result_pld

    # TODO: factor this out into a `.highlevel` API-wrapper that uses
    # a single `.open_context()` call underneath.
    async def result(
        self,
        *args,
        **kwargs,
    ) -> Any|Exception:
        typname: str = type(self).__name__
        log.warning(
            f'`{typname}.result()` is DEPRECATED!\n'
            f'Use `{typname}.wait_for_result()` instead!\n'
        )
        return await self.wait_for_result(
            *args,
            **kwargs,
        )

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
        timeout: float | None = None,

    ) -> bool:
        '''
        Cancel the actor runtime (and thus process) on the far
        end of this portal.

        **NOTE** THIS CANCELS THE ENTIRE RUNTIME AND THE
        SUBPROCESS, it DOES NOT just cancel the remote task. If you
        want to have a handle to cancel a remote ``tri.Task`` look
        at `.open_context()` and the definition of
        `._context.Context.cancel()` which CAN be used for this
        purpose.

        '''
        __runtimeframe__: int = 1  # noqa

        chan: Channel = self.channel
        if not chan.connected():
            log.runtime(
                'This channel is already closed, skipping cancel request..'
            )
            return False

        reminfo: str = (
            f'c)=> {self.channel.uid}\n'
            f'  |_{chan}\n'
        )
        log.cancel(
            f'Requesting actor-runtime cancel for peer\n\n'
            f'{reminfo}'
        )

        # XXX the one spot we set it?
        self.channel._cancel_called: bool = True
        try:
            # send cancel cmd - might not get response
            # XXX: sure would be nice to make this work with
            # a proper shield
            with trio.move_on_after(
                timeout
                or
                self.cancel_timeout
            ) as cs:
                cs.shield: bool = True
                await self.run_from_ns(
                    'self',
                    'cancel',
                )
                return True

            if cs.cancelled_caught:
                # may timeout and we never get an ack (obvi racy)
                # but that doesn't mean it wasn't cancelled.
                log.debug(
                    'May have failed to cancel peer?\n'
                    f'{reminfo}'
                )

            # if we get here some weird cancellation case happened
            return False

        except (
            trio.ClosedResourceError,
            trio.BrokenResourceError,
        ):
            log.debug(
                'IPC chan for actor already closed or broken?\n\n'
                f'{self.channel.uid}\n'
                f' |_{self.channel}\n'
            )
            return False

    # TODO: do we still need this for low level `Actor`-runtime
    # method calls or can we also remove it?
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
          should only ever be used for `Actor` (method) runtime
          internals!

        '''
        __runtimeframe__: int = 1  # noqa
        nsf = NamespacePath(
            f'{namespace_path}:{function_name}'
        )
        ctx: Context = await self.actor.start_remote_task(
            chan=self.channel,
            nsf=nsf,
            kwargs=kwargs,
            portal=self,
        )
        return await ctx._pld_rx.recv_pld(
            ipc=ctx,
            expect_msg=Return,
        )

    # TODO: factor this out into a `.highlevel` API-wrapper that uses
    # a single `.open_context()` call underneath.
    async def run(
        self,
        func: str,
        fn_name: str|None = None,
        **kwargs

    ) -> Any:
        '''
        Submit a remote function to be scheduled and run by actor, in
        a new task, wrap and return its (stream of) result(s).

        This is a blocking call and returns either a value from the
        remote rpc task or a local async generator instance.

        '''
        __runtimeframe__: int = 1  # noqa

        if isinstance(func, str):
            warnings.warn(
                "`Portal.run(namespace: str, funcname: str)` is now"
                "deprecated, pass a function reference directly instead\n"
                "If you still want to run a remote function by name use"
                "`Portal.run_from_ns()`",
                DeprecationWarning,
                stacklevel=2,
            )
            fn_mod_path: str = func
            assert isinstance(fn_name, str)
            nsf = NamespacePath(f'{fn_mod_path}:{fn_name}')

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

            nsf = NamespacePath.from_ref(func)

        ctx = await self.actor.start_remote_task(
            self.channel,
            nsf=nsf,
            kwargs=kwargs,
            portal=self,
        )
        return await ctx._pld_rx.recv_pld(
            ipc=ctx,
            expect_msg=Return,
        )

    # TODO: factor this out into a `.highlevel` API-wrapper that uses
    # a single `.open_context()` call underneath.
    @acm
    async def open_stream_from(
        self,
        async_gen_func: Callable,  # typing: ignore
        **kwargs,

    ) -> AsyncGenerator[MsgStream, None]:
        '''
        Legacy one-way streaming API.

        TODO: re-impl on top `Portal.open_context()` + an async gen
        around `Context.open_stream()`.

        '''
        __runtimeframe__: int = 1  # noqa

        if not inspect.isasyncgenfunction(async_gen_func):
            if not (
                inspect.iscoroutinefunction(async_gen_func) and
                getattr(async_gen_func, '_tractor_stream_function', False)
            ):
                raise TypeError(
                    f'{async_gen_func} must be an async generator function!')

        ctx: Context = await self.actor.start_remote_task(
            self.channel,
            nsf=NamespacePath.from_ref(async_gen_func),
            kwargs=kwargs,
            portal=self,
        )

        # ensure receive-only stream entrypoint
        assert ctx._remote_func_type == 'asyncgen'

        try:
            # deliver receive only stream
            async with MsgStream(
                ctx=ctx,
                rx_chan=ctx._rx_chan,
            ) as stream:
                self._streams.add(stream)
                ctx._stream = stream
                yield stream

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
            self._streams.remove(stream)

    # NOTE: impl is found in `._context`` mod to make
    # reading/groking the details simpler code-org-wise. This
    # method does not have to be used over that `@acm` module func
    # directly, it is for conventience and from the original API
    # design.
    open_context = open_context_from_portal


@dataclass
class LocalPortal:
    '''
    A 'portal' to a local ``Actor``.

    A compatibility shim for normal portals but for invoking functions
    using an in process actor instance.

    '''
    actor: 'Actor'  # type: ignore # noqa
    channel: Channel

    async def run_from_ns(
        self,
        ns: str,
        func_name: str,
        **kwargs,
    ) -> Any:
        '''
        Run a requested local function from a namespace path and
        return it's result.

        '''
        obj = self.actor if ns == 'self' else importlib.import_module(ns)
        func = getattr(obj, func_name)
        return await func(**kwargs)


@acm
async def open_portal(

    channel: Channel,
    tn: trio.Nursery|None = None,
    start_msg_loop: bool = True,
    shield: bool = False,

) -> AsyncGenerator[Portal, None]:
    '''
    Open a ``Portal`` through the provided ``channel``.

    Spawns a background task to handle RPC processing, normally
    done by the actor-runtime implicitly via a call to
    `._rpc.process_messages()`. just after connection establishment.

    '''
    actor = current_actor()
    assert actor
    was_connected: bool = False

    async with maybe_open_nursery(
        tn,
        shield=shield,
        strict_exception_groups=False,
        # ^XXX^ TODO? soo roll our own then ??
        # -> since we kinda want the "if only one `.exception` then
        # just raise that" interface?
    ) as tn:

        if not channel.connected():
            await channel.connect()
            was_connected = True

        if channel.uid is None:
            await actor._do_handshake(channel)

        msg_loop_cs: trio.CancelScope|None = None
        if start_msg_loop:
            from ._runtime import process_messages
            msg_loop_cs = await tn.start(
                partial(
                    process_messages,
                    actor,
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
                await channel.aclose()

            # cancel background msg loop task
            if msg_loop_cs is not None:
                msg_loop_cs.cancel()

            tn.cancel_scope.cancel()
