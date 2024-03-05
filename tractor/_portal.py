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
import importlib
import inspect
from typing import (
    Any,
    Callable,
    AsyncGenerator,
    Type,
)
from functools import partial
from dataclasses import dataclass
import warnings

import trio
from async_generator import asynccontextmanager

from .trionics import maybe_open_nursery
from .devx import (
    # acquire_debug_lock,
    # pause,
    maybe_wait_for_debugger,
)
from ._state import (
    current_actor,
    debug_mode,
)
from ._ipc import Channel
from .log import get_logger
from .msg import NamespacePath
from ._exceptions import (
    InternalError,
    _raise_from_no_key_in_msg,
    unpack_error,
    NoResult,
    ContextCancelled,
    RemoteActorError,
)
from ._context import (
    Context,
)
from ._streaming import (
    MsgStream,
)


log = get_logger(__name__)


# TODO: rename to `unwrap_result()` and use
# `._raise_from_no_key_in_msg()` (after tweak to
# accept a `chan: Channel` arg) in key block!
def _unwrap_msg(
    msg: dict[str, Any],
    channel: Channel,

    hide_tb: bool = True,

) -> Any:
    '''
    Unwrap a final result from a `{return: <Any>}` IPC msg.

    '''
    __tracebackhide__: bool = hide_tb

    try:
        return msg['return']
    except KeyError as ke:

        # internal error should never get here
        assert msg.get('cid'), (
            "Received internal error at portal?"
        )

        raise unpack_error(
            msg,
            channel
        ) from ke


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

    def __init__(self, channel: Channel) -> None:
        self.chan = channel
        # during the portal's lifetime
        self._result_msg: dict|None = None

        # When set to a ``Context`` (when _submit_for_result is called)
        # it is expected that ``result()`` will be awaited at some
        # point.
        self._expect_result: Context | None = None
        self._streams: set[MsgStream] = set()
        self.actor = current_actor()

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

    async def _submit_for_result(
        self,
        ns: str,
        func: str,
        **kwargs
    ) -> None:

        assert self._expect_result is None, (
            "A pending main result has already been submitted"
        )

        self._expect_result = await self.actor.start_remote_task(
            self.channel,
            nsf=NamespacePath(f'{ns}:{func}'),
            kwargs=kwargs
        )

    async def _return_once(
        self,
        ctx: Context,

    ) -> dict[str, Any]:

        assert ctx._remote_func_type == 'asyncfunc'  # single response
        msg: dict = await ctx._recv_chan.receive()
        return msg

    async def result(self) -> Any:
        '''
        Return the result(s) from the remote actor's "main" task.

        '''
        # __tracebackhide__ = True
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

        return _unwrap_msg(
            self._result_msg,
            self.channel,
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
        chan: Channel = self.channel
        if not chan.connected():
            log.runtime(
                'This channel is already closed, skipping cancel request..'
            )
            return False

        reminfo: str = (
            f'`Portal.cancel_actor()` => {self.channel.uid}\n'
            f' |_{chan}\n'
        )
        log.cancel(
            f'Sending runtime `.cancel()` request to peer\n\n'
            f'{reminfo}'
        )

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
        nsf = NamespacePath(
            f'{namespace_path}:{function_name}'
        )
        ctx = await self.actor.start_remote_task(
            chan=self.channel,
            nsf=nsf,
            kwargs=kwargs,
        )
        ctx._portal = self
        msg = await self._return_once(ctx)
        return _unwrap_msg(
            msg,
            self.channel,
        )

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

    ) -> AsyncGenerator[MsgStream, None]:

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
        )
        ctx._portal = self

        # ensure receive-only stream entrypoint
        assert ctx._remote_func_type == 'asyncgen'

        try:
            # deliver receive only stream
            async with MsgStream(
                ctx=ctx,
                rx_chan=ctx._recv_chan,
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

    # TODO: move this impl to `._context` mod and
    # instead just bind it here as a method so that the logic
    # for ctx stuff stays all in one place (instead of frickin
    # having to open this file in tandem every gd time!!! XD)
    #
    @asynccontextmanager
    async def open_context(

        self,
        func: Callable,

        allow_overruns: bool = False,

        # TODO: if we set this the wrapping `@acm` body will
        # still be shown (awkwardly) on pdb REPL entry. Ideally
        # we can similarly annotate that frame to NOT show?
        hide_tb: bool = False,

        # proxied to RPC
        **kwargs,

    ) -> AsyncGenerator[tuple[Context, Any], None]:
        '''
        Open an inter-actor "task context"; a remote task is
        scheduled and cancel-scope-state-linked to a `trio.run()` across
        memory boundaries in another actor's runtime.

        This is an `@acm` API which allows for deterministic setup
        and teardown of a remotely scheduled task in another remote
        actor. Once opened, the 2 now "linked" tasks run completely
        in parallel in each actor's runtime with their enclosing
        `trio.CancelScope`s kept in a synced state wherein if
        either side errors or cancels an equivalent error is
        relayed to the other side via an SC-compat IPC protocol.

        The yielded `tuple` is a pair delivering a `tractor.Context`
        and any first value "sent" by the "callee" task via a call
        to `Context.started(<value: Any>)`; this side of the
        context does not unblock until the "callee" task calls
        `.started()` in similar style to `trio.Nursery.start()`.
        When the "callee" (side that is "called"/started by a call
        to *this* method) returns, the caller side (this) unblocks
        and any final value delivered from the other end can be
        retrieved using the `Contex.result()` api.

        The yielded ``Context`` instance further allows for opening
        bidirectional streams, explicit cancellation and
        structurred-concurrency-synchronized final result-msg
        collection. See ``tractor.Context`` for more details.

        '''
        __tracebackhide__: bool = hide_tb

        # conduct target func method structural checks
        if not inspect.iscoroutinefunction(func) and (
            getattr(func, '_tractor_contex_function', False)
        ):
            raise TypeError(
                f'{func} must be an async generator function!')

        # TODO: i think from here onward should probably
        # just be factored into an `@acm` inside a new
        # a new `_context.py` mod.
        nsf = NamespacePath.from_ref(func)

        ctx: Context = await self.actor.start_remote_task(
            self.channel,
            nsf=nsf,
            kwargs=kwargs,

            # NOTE: it's imporant to expose this since you might
            # get the case where the parent who opened the context does
            # not open a stream until after some slow startup/init
            # period, in which case when the first msg is read from
            # the feeder mem chan, say when first calling
            # `Context.open_stream(allow_overruns=True)`, the overrun condition will be
            # raised before any ignoring of overflow msgs can take
            # place..
            allow_overruns=allow_overruns,
        )

        assert ctx._remote_func_type == 'context'
        msg: dict = await ctx._recv_chan.receive()

        try:
            # the "first" value here is delivered by the callee's
            # ``Context.started()`` call.
            first: Any = msg['started']
            ctx._started_called: bool = True

        except KeyError as src_error:
            _raise_from_no_key_in_msg(
                ctx=ctx,
                msg=msg,
                src_err=src_error,
                log=log,
                expect_key='started',
            )

        ctx._portal: Portal = self
        uid: tuple = self.channel.uid
        cid: str = ctx.cid

        # placeholder for any exception raised in the runtime
        # or by user tasks which cause this context's closure.
        scope_err: BaseException|None = None
        ctxc_from_callee: ContextCancelled|None = None
        try:
            async with trio.open_nursery() as nurse:

                # NOTE: used to start overrun queuing tasks
                ctx._scope_nursery: trio.Nursery = nurse
                ctx._scope: trio.CancelScope = nurse.cancel_scope

                # deliver context instance and .started() msg value
                # in enter tuple.
                yield ctx, first

                # ??TODO??: do we still want to consider this or is
                # the `else:` block handling via a `.result()`
                # call below enough??
                # -[ ] pretty sure `.result()` internals do the
                # same as our ctxc handler below so it ended up
                # being same (repeated?) behaviour, but ideally we
                # wouldn't have that duplication either by somehow
                # factoring the `.result()` handler impl in a way
                # that we can re-use it around the `yield` ^ here
                # or vice versa?
                #
                # NOTE: between the caller exiting and arriving
                # here the far end may have sent a ctxc-msg or
                # other error, so check for it here immediately
                # and maybe raise so as to engage the ctxc
                # handling block below!
                #
                # if re := ctx._remote_error:
                #     maybe_ctxc: ContextCancelled|None = ctx._maybe_raise_remote_err(
                #         re,
                #         # TODO: do we want this to always raise?
                #         # - means that on self-ctxc, if/when the
                #         #   block is exited before the msg arrives
                #         #   but then the msg during __exit__
                #         #   calling we may not activate the
                #         #   ctxc-handler block below? should we
                #         #   be?
                #         # - if there's a remote error that arrives
                #         #   after the child has exited, we won't
                #         #   handle until the `finally:` block
                #         #   where `.result()` is always called,
                #         #   again in which case we handle it
                #         #   differently then in the handler block
                #         #   that would normally engage from THIS
                #         #   block?
                #         raise_ctxc_from_self_call=True,
                #     )
                #     ctxc_from_callee = maybe_ctxc

                # when in allow_overruns mode there may be
                # lingering overflow sender tasks remaining?
                if nurse.child_tasks:
                    # XXX: ensure we are in overrun state
                    # with ``._allow_overruns=True`` bc otherwise
                    # there should be no tasks in this nursery!
                    if (
                        not ctx._allow_overruns
                        or len(nurse.child_tasks) > 1
                    ):
                        raise InternalError(
                            'Context has sub-tasks but is '
                            'not in `allow_overruns=True` mode!?'
                        )

                    # ensure we cancel all overflow sender
                    # tasks started in the nursery when
                    # `._allow_overruns == True`.
                    #
                    # NOTE: this means `._scope.cancelled_caught`
                    # will prolly be set! not sure if that's
                    # non-ideal or not ???
                    ctx._scope.cancel()

        # XXX NOTE XXX: maybe shield against
        # self-context-cancellation (which raises a local
        # `ContextCancelled`) when requested (via
        # `Context.cancel()`) by the same task (tree) which entered
        # THIS `.open_context()`.
        #
        # NOTE: There are 2 operating cases for a "graceful cancel"
        # of a `Context`. In both cases any `ContextCancelled`
        # raised in this scope-block came from a transport msg
        # relayed from some remote-actor-task which our runtime set
        # as to `Context._remote_error`
        #
        # the CASES:
        #
        # - if that context IS THE SAME ONE that called
        #   `Context.cancel()`, we want to absorb the error
        #   silently and let this `.open_context()` block to exit
        #   without raising, ideally eventually receiving the ctxc
        #   ack msg thus resulting in `ctx.cancel_acked == True`.
        #
        # - if it is from some OTHER context (we did NOT call
        #   `.cancel()`), we want to re-RAISE IT whilst also
        #   setting our own ctx's "reason for cancel" to be that
        #   other context's cancellation condition; we set our
        #   `.canceller: tuple[str, str]` to be same value as
        #   caught here in a `ContextCancelled.canceller`.
        #
        # AGAIN to restate the above, there are 2 cases:
        #
        # 1-some other context opened in this `.open_context()`
        #   block cancelled due to a self or peer cancellation
        #   request in which case we DO let the error bubble to the
        #   opener.
        #
        # 2-THIS "caller" task somewhere invoked `Context.cancel()`
        #   and received a `ContextCanclled` from the "callee"
        #   task, in which case we mask the `ContextCancelled` from
        #   bubbling to this "caller" (much like how `trio.Nursery`
        #   swallows any `trio.Cancelled` bubbled by a call to
        #   `Nursery.cancel_scope.cancel()`)
        except ContextCancelled as ctxc:
            scope_err = ctxc
            ctxc_from_callee = ctxc

            # XXX TODO XXX: FIX THIS debug_mode BUGGGG!!!
            # using this code and then resuming the REPL will
            # cause a SIGINT-ignoring HANG!
            # -> prolly due to a stale debug lock entry..
            # -[ ] USE `.stackscope` to demonstrate that (possibly
            #   documenting it as a definittive example of
            #   debugging the tractor-runtime itself using it's
            #   own `.devx.` tooling!
            # 
            # await pause()

            # CASE 2: context was cancelled by local task calling
            # `.cancel()`, we don't raise and the exit block should
            # exit silently.
            if (
                ctx._cancel_called
                and
                ctxc is ctx._remote_error
                and
                ctxc.canceller == self.actor.uid
            ):
                log.cancel(
                    f'Context (cid=[{ctx.cid[-6:]}..] cancelled gracefully with:\n'
                    f'{ctxc}'
                )
            # CASE 1: this context was never cancelled via a local
            # task (tree) having called `Context.cancel()`, raise
            # the error since it was caused by someone else
            # -> probably a remote peer!
            else:
                raise

        # the above `._scope` can be cancelled due to:
        # 1. an explicit self cancel via `Context.cancel()` or
        #    `Actor.cancel()`,
        # 2. any "callee"-side remote error, possibly also a cancellation
        #    request by some peer,
        # 3. any "caller" (aka THIS scope's) local error raised in the above `yield`
        except (
            # CASE 3: standard local error in this caller/yieldee
            Exception,

            # CASES 1 & 2: can manifest as a `ctx._scope_nursery`
            # exception-group of,
            #
            # 1.-`trio.Cancelled`s, since
            #   `._scope.cancel()` will have been called
            #   (transitively by the runtime calling
            #   `._deliver_msg()`) and any `ContextCancelled`
            #   eventually absorbed and thus absorbed/supressed in
            #   any `Context._maybe_raise_remote_err()` call.
            #
            # 2.-`BaseExceptionGroup[ContextCancelled | RemoteActorError]`
            #    from any error delivered from the "callee" side
            #    AND a group-exc is only raised if there was > 1
            #    tasks started *here* in the "caller" / opener
            #    block. If any one of those tasks calls
            #    `.result()` or `MsgStream.receive()`
            #    `._maybe_raise_remote_err()` will be transitively
            #    called and the remote error raised causing all
            #    tasks to be cancelled.
            #    NOTE: ^ this case always can happen if any
            #    overrun handler tasks were spawned!
            BaseExceptionGroup,

            trio.Cancelled,  # NOTE: NOT from inside the ctx._scope
            KeyboardInterrupt,

        ) as caller_err:
            scope_err = caller_err

            # XXX: ALWAYS request the context to CANCEL ON any ERROR.
            # NOTE: `Context.cancel()` is conversely NEVER CALLED in
            # the `ContextCancelled` "self cancellation absorbed" case
            # handled in the block above ^^^ !!
            log.cancel(
                'Context terminated due to\n\n'
                f'{caller_err}\n'
            )

            if debug_mode():
                # async with acquire_debug_lock(self.actor.uid):
                #     pass
                # TODO: factor ^ into below for non-root cases?
                was_acquired: bool = await maybe_wait_for_debugger(
                    header_msg=(
                        'Delaying `ctx.cancel()` until debug lock '
                        'acquired..\n'
                    ),
                )
                if was_acquired:
                    log.pdb(
                        'Acquired debug lock! '
                        'Calling `ctx.cancel()`!\n'
                    )


            # we don't need to cancel the callee if it already
            # told us it's cancelled ;p
            if ctxc_from_callee is None:
                try:
                    await ctx.cancel()
                except (
                    trio.BrokenResourceError,
                    trio.ClosedResourceError,
                ):
                    log.warning(
                        'IPC connection for context is broken?\n'
                        f'task:{cid}\n'
                        f'actor:{uid}'
                    )

            raise  # duh

        # no local scope error, the "clean exit with a result" case.
        else:
            if ctx.chan.connected():
                log.runtime(
                    'Waiting on final context result for\n'
                    f'peer: {uid}\n'
                    f'|_{ctx._task}\n'
                )
                # XXX NOTE XXX: the below call to
                # `Context.result()` will ALWAYS raise
                # a `ContextCancelled` (via an embedded call to
                # `Context._maybe_raise_remote_err()`) IFF
                # a `Context._remote_error` was set by the runtime
                # via a call to
                # `Context._maybe_cancel_and_set_remote_error()`.
                # As per `Context._deliver_msg()`, that error IS
                # ALWAYS SET any time "callee" side fails and causes "caller
                # side" cancellation via a `ContextCancelled` here.
                try:
                    result_or_err: Exception|Any = await ctx.result()
                except BaseException as berr:
                    # on normal teardown, if we get some error
                    # raised in `Context.result()` we still want to
                    # save that error on the ctx's state to
                    # determine things like `.cancelled_caught` for
                    # cases where there was remote cancellation but
                    # this task didn't know until final teardown
                    # / value collection.
                    scope_err = berr
                    raise

                # yes! this worx Bp
                # from .devx import _debug
                # await _debug.pause()

                # an exception type boxed in a `RemoteActorError`
                # is returned (meaning it was obvi not raised)
                # that we want to log-report on.
                msgdata: str|None = getattr(
                    result_or_err,
                    'msgdata',
                    None
                )
                match (msgdata, result_or_err):
                    case (
                        {'tb_str': tbstr},
                        ContextCancelled(),
                    ):
                        log.cancel(tbstr)

                    case (
                        {'tb_str': tbstr},
                        RemoteActorError(),
                    ):
                        log.exception(
                            'Context remotely errored!\n'
                            f'<= peer: {uid}\n'
                            f'  |_ {nsf}()\n\n'

                            f'{tbstr}'
                        )
                    case (None, _):
                        log.runtime(
                            'Context returned final result from callee task:\n'
                            f'<= peer: {uid}\n'
                            f'  |_ {nsf}()\n\n'

                            f'`{result_or_err}`\n'
                        )

        finally:
            # XXX: (MEGA IMPORTANT) if this is a root opened process we
            # wait for any immediate child in debug before popping the
            # context from the runtime msg loop otherwise inside
            # ``Actor._push_result()`` the msg will be discarded and in
            # the case where that msg is global debugger unlock (via
            # a "stop" msg for a stream), this can result in a deadlock
            # where the root is waiting on the lock to clear but the
            # child has already cleared it and clobbered IPC.
            await maybe_wait_for_debugger()

            # though it should be impossible for any tasks
            # operating *in* this scope to have survived
            # we tear down the runtime feeder chan last
            # to avoid premature stream clobbers.
            if (
                (rxchan := ctx._recv_chan)

                # maybe TODO: yes i know the below check is
                # touching `trio` memchan internals..BUT, there are
                # only a couple ways to avoid a `trio.Cancelled`
                # bubbling from the `.aclose()` call below:
                #
                # - catch and mask it via the cancel-scope-shielded call
                #   as we are rn (manual and frowned upon) OR,
                # - specially handle the case where `scope_err` is
                #   one of {`BaseExceptionGroup`, `trio.Cancelled`}
                #   and then presume that the `.aclose()` call will
                #   raise a `trio.Cancelled` and just don't call it
                #   in those cases..
                #
                # that latter approach is more logic, LOC, and more
                # convoluted so for now stick with the first
                # psuedo-hack-workaround where we just try to avoid
                # the shielded call as much as we can detect from
                # the memchan's `._closed` state..
                #
                # XXX MOTIVATION XXX-> we generally want to raise
                # any underlying actor-runtime/internals error that
                # surfaces from a bug in tractor itself so it can
                # be easily detected/fixed AND, we also want to
                # minimize noisy runtime tracebacks (normally due
                # to the cross-actor linked task scope machinery
                # teardown) displayed to user-code and instead only
                # displaying `ContextCancelled` traces where the
                # cause of crash/exit IS due to something in
                # user/app code on either end of the context.
                and not rxchan._closed
            ):
                # XXX NOTE XXX: and again as per above, we mask any
                # `trio.Cancelled` raised here so as to NOT mask
                # out any exception group or legit (remote) ctx
                # error that sourced from the remote task or its
                # runtime.
                #
                # NOTE: further, this should be the only place the
                # underlying feeder channel is
                # once-and-only-CLOSED!
                with trio.CancelScope(shield=True):
                    await ctx._recv_chan.aclose()

            # XXX: we always raise remote errors locally and
            # generally speaking mask runtime-machinery related
            # multi-`trio.Cancelled`s. As such, any `scope_error`
            # which was the underlying cause of this context's exit
            # should be stored as the `Context._local_error` and
            # used in determining `Context.cancelled_caught: bool`.
            if scope_err is not None:
                ctx._local_error: BaseException = scope_err
                etype: Type[BaseException] = type(scope_err)

                # CASE 2
                if (
                    ctx._cancel_called
                    and ctx.cancel_acked
                ):
                    log.cancel(
                        'Context cancelled by caller task\n'
                        f'|_{ctx._task}\n\n'

                        f'{repr(scope_err)}\n'
                    )

                # TODO: should we add a `._cancel_req_received`
                # flag to determine if the callee manually called
                # `ctx.cancel()`?
                # -[ ] going to need a cid check no?

                # CASE 1
                else:
                    outcome_str: str = ctx.repr_outcome(
                        show_error_fields=True,
                        # type_only=True,
                    )
                    log.cancel(
                        f'Context terminated due to local scope error:\n\n'
                        f'{ctx.chan.uid} => {outcome_str}\n'
                    )

            # XXX: (MEGA IMPORTANT) if this is a root opened process we
            # wait for any immediate child in debug before popping the
            # context from the runtime msg loop otherwise inside
            # ``Actor._push_result()`` the msg will be discarded and in
            # the case where that msg is global debugger unlock (via
            # a "stop" msg for a stream), this can result in a deadlock
            # where the root is waiting on the lock to clear but the
            # child has already cleared it and clobbered IPC.

            # FINALLY, remove the context from runtime tracking and
            # exit!
            log.runtime(
                'Removing IPC ctx opened with peer\n'
                f'{uid}\n'
                f'|_{ctx}\n'
            )
            self.actor._contexts.pop(
                (uid, cid),
                None,
            )


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
    nursery: trio.Nursery|None = None,
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

        msg_loop_cs: trio.CancelScope|None = None
        if start_msg_loop:
            from ._runtime import process_messages
            msg_loop_cs = await nursery.start(
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
                # gracefully signal remote channel-msg loop
                await channel.send(None)
                # await channel.aclose()

            # cancel background msg loop task
            if msg_loop_cs:
                msg_loop_cs.cancel()

            nursery.cancel_scope.cancel()
