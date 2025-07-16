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
Message stream types and APIs.

The machinery and types behind ``Context.open_stream()``

'''
from __future__ import annotations
from contextlib import asynccontextmanager as acm
import inspect
from pprint import pformat
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    AsyncIterator,
    TYPE_CHECKING,
)
import warnings

import trio

from ._exceptions import (
    ContextCancelled,
    RemoteActorError,
)
from .log import get_logger
from .trionics import (
    broadcast_receiver,
    BroadcastReceiver,
)
from tractor.msg import (
    Error,
    Return,
    Stop,
    MsgType,
    PayloadT,
    Yield,
)

if TYPE_CHECKING:
    from ._runtime import Actor
    from ._context import Context
    from .ipc import Channel


log = get_logger(__name__)


# TODO: the list
# - generic typing like trio's receive channel but with msgspec
#   messages? class ReceiveChannel(AsyncResource, Generic[ReceiveType]):
# - use __slots__ on ``Context``?

class MsgStream(trio.abc.Channel):
    '''
    A bidirectional message stream for receiving logically sequenced
    values over an inter-actor IPC `Channel`.



    Termination rules:

    - on cancellation the stream is **not** implicitly closed and the
      surrounding ``Context`` is expected to handle how that cancel
      is relayed to any task on the remote side.
    - if the remote task signals the end of a stream the
      ``ReceiveChannel`` semantics dictate that a ``StopAsyncIteration``
      to terminate the local ``async for``.

    '''
    def __init__(
        self,
        ctx: Context,  # typing: ignore # noqa
        rx_chan: trio.MemoryReceiveChannel,
        _broadcaster: BroadcastReceiver|None = None,

    ) -> None:
        self._ctx = ctx
        self._rx_chan = rx_chan
        self._broadcaster = _broadcaster

        # any actual IPC msg which is effectively an `EndOfStream`
        self._stop_msg: bool|Stop = False

        # flag to denote end of stream
        self._eoc: bool|trio.EndOfChannel = False
        self._closed: bool|trio.ClosedResourceError = False

    def is_eoc(self) -> bool|trio.EndOfChannel:
        return self._eoc

    @property
    def ctx(self) -> Context:
        '''
        A read-only ref to this stream's inter-actor-task `Context`.

        '''
        return self._ctx

    @property
    def chan(self) -> Channel:
        '''
        Ref to the containing `Context`'s transport `Channel`.

        '''
        return self._ctx.chan

    # TODO: could we make this a direct method bind to `PldRx`?
    # -> receive_nowait = PldRx.recv_pld
    # |_ means latter would have to accept `MsgStream`-as-`self`?
    #  => should be fine as long as,
    #  -[ ] both define `._rx_chan`
    #  -[ ] .ctx is bound into `PldRx` using a `@cm`?
    #
    # delegate directly to underlying mem channel
    def receive_nowait(
        self,
        expect_msg: MsgType = Yield,
    ) -> PayloadT:
        ctx: Context = self._ctx
        (
            msg,
            pld,
        ) = ctx._pld_rx.recv_msg_nowait(
            ipc=self,
            expect_msg=expect_msg,
        )

        # ?TODO, maybe factor this into a hyper-common `unwrap_pld()`
        #
        match msg:

            # XXX, these never seems to ever hit? cool?
            case Stop():
                log.cancel(
                    f'Msg-stream was ended via stop msg\n'
                    f'{msg}'
                )
            case Error():
                log.error(
                    f'Msg-stream was ended via error msg\n'
                    f'{msg}'
                )

            # XXX NOTE, always set any final result on the ctx to
            # avoid teardown race conditions where previously this msg
            # would be consumed silently (by `.aclose()` doing its
            # own "msg drain loop" but WITHOUT those `drained: lists[MsgType]`
            # being post-close-processed!
            #
            # !!TODO, see the equiv todo-comment in `.receive()`
            # around the `if drained:` where we should prolly
            # ACTUALLY be doing this post-close processing??
            #
            case Return(pld=pld):
                log.warning(
                    f'Msg-stream final result msg for IPC ctx?\n'
                    f'{msg}'
                )
                # XXX TODO, this **should be covered** by higher
                # scoped runtime-side method calls such as
                # `Context._deliver_msg()`, so you should never
                # really see the warning above or else something
                # racy/out-of-order is likely going on between
                # actor-runtime-side push tasks and the user-app-side
                # consume tasks!
                # -[ ] figure out that set of race cases and fix!
                # -[ ] possibly return the `msg` given an input
                #     arg-flag is set so we can process the `Return`
                #     from the `.aclose()` caller?
                #
                # breakpoint()  # to debug this RACE CASE!
                ctx._result = pld
                ctx._outcome_msg = msg

        return pld

    # XXX NOTE, this is left private because in `.subscribe()` usage
    # we rebind the public `.recieve()` to a `BroadcastReceiver` but
    # on `.subscribe().__aexit__()`, for the first task which enters,
    # we want to revert to this msg-stream-instance's method since
    # mult-task-tracking provided by the b-caster is then no longer
    # necessary.
    #
    async def _receive(
        self,
        hide_tb: bool = False,
    ):
        '''
        Receive a single msg from the IPC transport, the next in
        sequence sent by the far end task (possibly in order as
        determined by the underlying protocol).

        '''
        __tracebackhide__: bool = hide_tb

        # NOTE FYI: `trio.ReceiveChannel` implements EOC handling as
        # follows (aka uses it to gracefully exit async for loops):
        #
        # async def __anext__(self) -> ReceiveType:
        #     try:
        #         return await self.receive()
        #     except trio.EndOfChannel:
        #         raise StopAsyncIteration
        #
        # see `.aclose()` for notes on the old behaviour prior to
        # introducing this
        if self._eoc:
            raise self._eoc

        if self._closed:
            raise self._closed

        src_err: Exception|None = None  # orig tb
        try:
            ctx: Context = self._ctx
            pld = await ctx._pld_rx.recv_pld(
                ipc=self,
                expect_msg=Yield,
            )
            return pld

        # XXX: the stream terminates on either of:
        # - `self._rx_chan.receive()` raising  after manual closure
        #   by the rpc-runtime,
        #   OR
        # - via a `Stop`-msg received from remote peer task.
        #   NOTE
        #   |_ previously this was triggered by calling
        #   `._rx_chan.aclose()` on the send side of the channel
        #   inside `Actor._deliver_ctx_payload()`, but now the 'stop'
        #   message handling gets delegated to `PldRFx.recv_pld()`
        #   internals.
        except trio.EndOfChannel as eoc:
            # a graceful stream finished signal
            self._eoc = eoc
            src_err = eoc

        # a `ClosedResourceError` indicates that the internal feeder
        # memory receive channel was closed likely by the runtime
        # after the associated transport-channel disconnected or
        # broke.
        except trio.ClosedResourceError as cre:  # by self._rx_chan.receive()
            src_err = cre
            log.warning(
                '`Context._rx_chan` was already closed?'
            )
            self._closed = cre

        # when the send is closed we assume the stream has
        # terminated and signal this local iterator to stop
        drained: list[Exception|dict] = await self.aclose()
        if drained:
        #  ^^^^^^^^TODO? pass these to the `._ctx._drained_msgs:
        #  deque` and then iterate them as part of any
        #  `.wait_for_result()` call?
        #
        # -[ ] move the match-case processing from
        #     `.receive_nowait()` instead to right here, use it from
        #     a for msg in drained:` post-proc loop?
        #
            log.warning(
                'Drained context msgs during closure\n\n'
                f'{drained}'
            )

        # NOTE XXX: if the context was cancelled or remote-errored
        # but we received the stream close msg first, we
        # probably want to instead raise the remote error
        # over the end-of-stream connection error since likely
        # the remote error was the source cause?
        # ctx: Context = self._ctx
        ctx.maybe_raise(
            raise_ctxc_from_self_call=True,
            from_src_exc=src_err,
        )

        # propagate any error but hide low-level frame details from
        # the caller by default for console/debug-REPL noise
        # reduction.
        if (
            hide_tb
            and (

                # XXX NOTE special conditions: don't reraise on
                # certain stream-specific internal error types like,
                #
                # - `trio.EoC` since we want to use the exact instance
                #   to ensure that it is the error that bubbles upward
                #   for silent absorption by `Context.open_stream()`.
                not self._eoc

                # - `RemoteActorError` (or subtypes like ctxc)
                #    since we want to present the error as though it is
                #    "sourced" directly from this `.receive()` call and
                #    generally NOT include the stack frames raised from
                #    inside the `PldRx` and/or the transport stack
                #    layers.
                or isinstance(src_err, RemoteActorError)
            )
        ):
            raise type(src_err)(*src_err.args) from src_err
        else:
            # for any non-graceful-EOC we want to NOT hide this frame
            if not self._eoc:
                __tracebackhide__: bool = False

            raise src_err

    receive = _receive

    async def aclose(self) -> list[Exception|dict]:
        '''
        Cancel associated remote actor task and local memory channel on
        close.

        Notes: 
         - REMEMBER that this is also called by `.__aexit__()` so
           careful consideration must be made to handle whatever
           internal stsate is mutated, particuarly in terms of
           draining IPC msgs!

         - more or less we try to maintain adherance to trio's `.aclose()` semantics:
           https://trio.readthedocs.io/en/stable/reference-io.html#trio.abc.AsyncResource.aclose
        '''
        # XXX NOTE XXX
        # it's SUPER IMPORTANT that we ensure we don't DOUBLE
        # DRAIN msgs on closure so avoid getting stuck handing on
        # the `._rx_chan` since we call this method on
        # `.__aexit__()` as well!!!
        # => SO ENSURE WE CATCH ALL TERMINATION STATES in this
        # block including the EoC..
        if self.closed:
            # this stream has already been closed so silently succeed as
            # per ``trio.AsyncResource`` semantics.
            # https://trio.readthedocs.io/en/stable/reference-io.html#trio.abc.AsyncResource.aclose
            # import tractor
            # await tractor.pause()
            return []

        ctx: Context = self._ctx
        drained: list[Exception|dict] = []
        while not drained:
            try:
                maybe_final_msg: Yield|Return = self.receive_nowait(
                    expect_msg=Yield|Return,
                )
                if maybe_final_msg:
                    log.debug(
                        'Drained un-processed stream msg:\n'
                        f'{pformat(maybe_final_msg)}'
                    )
                    # TODO: inject into parent `Context` buf?
                    drained.append(maybe_final_msg)

            # NOTE: we only need these handlers due to the
            # `.receive_nowait()` call above which may re-raise
            # one of these errors on a msg key error!

            except trio.WouldBlock as be:
                drained.append(be)
                break

            except trio.EndOfChannel as eoc:
                self._eoc: Exception = eoc
                drained.append(eoc)
                break

            except trio.ClosedResourceError as cre:
                self._closed = cre
                drained.append(cre)
                break

            except ContextCancelled as ctxc:
                # log.exception('GOT CTXC')
                log.cancel(
                    'Context was cancelled during stream closure:\n'
                    f'canceller: {ctxc.canceller}\n'
                    f'{pformat(ctxc.msgdata)}'
                )
                break

        # NOTE: this is super subtle IPC messaging stuff:
        # Relay stop iteration to far end **iff** we're
        # in bidirectional mode. If we're only streaming
        # *from* one side then that side **won't** have an
        # entry in `Actor._cids2qs` (maybe it should though?).
        # So any `yield` or `stop` msgs sent from the caller side
        # will cause key errors on the callee side since there is
        # no entry for a local feeder mem chan since the callee task
        # isn't expecting messages to be sent by the caller.
        # Thus, we must check that this context DOES NOT
        # have a portal reference to ensure this is indeed the callee
        # side and can relay a 'stop'.

        # In the bidirectional case, `Context.open_stream()` will create
        # the `Actor._cids2qs` entry from a call to
        # `Actor.get_context()` and will call us here to send the stop
        # msg in ``__aexit__()`` on teardown.
        try:
            # NOTE: if this call is cancelled we expect this end to
            # handle as though the stop was never sent (though if it
            # was it shouldn't matter since it's unlikely a user
            # will try to re-use a stream after attemping to close
            # it).
            with trio.CancelScope(shield=True):
                await self._ctx.send_stop()

        except (
            trio.BrokenResourceError,
            trio.ClosedResourceError
        ) as re:
            # the underlying channel may already have been pulled
            # in which case our stop message is meaningless since
            # it can't traverse the transport.
            log.warning(
                f'Stream was already destroyed?\n'
                f'actor: {ctx.chan.uid}\n'
                f'ctx id: {ctx.cid}'
            )
            drained.append(re)
            self._closed = re

        # if caught_eoc:
        #     # from .devx import debug
        #     # await debug.pause()
        #     with trio.CancelScope(shield=True):
        #         await rx_chan.aclose()

        if not self._eoc:
            this_side: str = self._ctx.side
            peer_side: str = self._ctx.peer_side
            message: str = (
                f'Stream self-closed by {this_side!r}-side before EoC from {peer_side!r}\n'
                # } bc a stream is a "scope"/msging-phase inside an IPC
                f'c}}>\n'
                f'  |_{self}\n'
            )
            if (
                (rx_chan := self._rx_chan)
                and
                (stats := rx_chan.statistics()).tasks_waiting_receive
            ):
                message += (
                    f'AND there is still reader tasks,\n'
                    f'\n'
                    f'{stats}\n'
                )

            log.cancel(message)
            self._eoc = trio.EndOfChannel(message)

        # ?XXX WAIT, why do we not close the local mem chan `._rx_chan` XXX?
        # => NO, DEFINITELY NOT! <=
        # if we're a bi-dir `MsgStream` BECAUSE this same
        # core-msg-loop mem recv-chan is used to deliver the
        # potential final result from the surrounding inter-actor
        # `Context` so we don't want to close it until that
        # context has run to completion.

        # XXX: Notes on old behaviour:
        # await rx_chan.aclose()

        # In the receive-only case, ``Portal.open_stream_from()`` used
        # to rely on this call explicitly on teardown such that a new
        # call to ``.receive()`` after ``rx_chan`` had been closed, would
        # result in us raising a ``trio.EndOfChannel`` (since we
        # remapped the ``trio.ClosedResourceError`). However, now if for some
        # reason the stream's consumer code tries to manually receive a new
        # value before ``.aclose()`` is called **but** the far end has
        # stopped `.receive()` **must** raise ``trio.EndofChannel`` in
        # order to avoid an infinite hang on ``.__anext__()``; this is
        # why we added ``self._eoc`` to denote stream closure indepedent
        # of ``rx_chan``.

        # In theory we could still use this old method and close the
        # underlying msg-loop mem chan as above and then **not** check
        # for ``self._eoc`` in ``.receive()`` (if for some reason we
        # think that check is a bottle neck - not likely) **but** then
        # we would need to map the resulting
        # ``trio.ClosedResourceError`` to a ``trio.EndOfChannel`` in
        # ``.receive()`` (as it originally was before bi-dir streaming
        # support) in order to trigger stream closure. The old behaviour
        # is arguably more confusing since we lose detection of the
        # runtime's closure of ``rx_chan`` in the case where we may
        # still need to consume msgs that are "in transit" from the far
        # end (eg. for ``Context.result()``).
        # self._closed = True
        return drained

    @property
    def closed(self) -> bool:

        rxc: bool = self._rx_chan._closed
        _closed: bool|Exception = self._closed
        _eoc: bool|trio.EndOfChannel = self._eoc
        if rxc or _closed or _eoc:
            log.runtime(
                f'`MsgStream` is already closed\n'
                f'{self}\n'
                f' |_cid: {self._ctx.cid}\n'
                f' |_rx_chan._closed: {type(rxc)} = {rxc}\n'
                f' |_closed: {type(_closed)} = {_closed}\n'
                f' |_eoc: {type(_eoc)} = {_eoc}'
            )
            return True
        return False

    @acm
    async def subscribe(
        self,

    ) -> AsyncIterator[BroadcastReceiver]:
        '''
        Allocate and return a ``BroadcastReceiver`` which delegates
        to this message stream.

        This allows multiple local tasks to receive each their own copy
        of this message stream.

        This operation is indempotent and and mutates this stream's
        receive machinery to copy and window-length-store each received
        value from the far end via the internally created broudcast
        receiver wrapper.

        '''
        # XXX NOTE, This operation was originally implemented as
        # indempotent and non-reversible, so you had to be **VERY**
        # aware of any (theoretical) overhead from the allocated
        # `BroadcastReceiver.receive()`.
        #
        # HOWEVER, NOw we do revert and de-alloc the ._broadcaster
        # when the final caller (task) exits.
        #
        bcast: BroadcastReceiver|None = None
        if self._broadcaster is None:

            bcast = self._broadcaster = broadcast_receiver(
                self,
                # use memory channel size by default
                self._rx_chan._state.max_buffer_size,  # type: ignore

                # TODO: can remove this kwarg right since
                # by default behaviour is to do this anyway?
                receive_afunc=self._receive,
            )

            # XXX NOTE, we override the original stream instance's
            # receive method to instead delegate to the broadcaster's
            # `.receive()` such that new subscribers (multiple
            # `trio.Task`s) will be copied received values and the
            # *first* task to enter here doesn't have to expect its original consumer(s)
            # to get a new broadcast rx handle; everything happens
            # underneath this iface seemlessly.
            #
            self.receive = bcast.receive  # type: ignore
            # seems there's no graceful way to type this with `mypy`?
            # https://github.com/python/mypy/issues/708

        # TODO, prevent re-entrant sub scope?
        # if self._broadcaster._closed:
        #     raise RuntimeError(
        #         'This stream

        try:
            aenter = self._broadcaster.subscribe()
            async with aenter as bstream:
                # ?TODO, move into test suite?
                assert bstream.key != self._broadcaster.key
                assert bstream._recv == self._broadcaster._recv

                # NOTE: we patch on a `.send()` to the bcaster so that the
                # caller can still conduct 2-way streaming using this
                # ``bstream`` handle transparently as though it was the msg
                # stream instance.
                bstream.send = self.send  # type: ignore

                # newly-allocated instance
                yield bstream

        finally:
            # XXX, the first-enterer task should, like all other
            # subs, close the first allocated bcrx, which adjusts the
            # common `bcrx.state`
            with trio.CancelScope(shield=True):
                if bcast is not None:
                    await bcast.aclose()

                # XXX, when the bcrx.state reports there are no more subs
                # we can revert to this obj's method, removing any
                # delegation overhead!
                if (
                    (orig_bcast := self._broadcaster)
                    and
                    not orig_bcast.state.subs
                ):
                    self.receive = self._receive
                    # self._broadcaster = None

    async def send(
        self,
        data: Any,

        hide_tb: bool = True,
    ) -> None:
        '''
        Send a message over this stream to the far end.

        '''
        __tracebackhide__: bool = hide_tb

        # raise any alreay known error immediately
        self._ctx.maybe_raise()
        if self._eoc:
            raise self._eoc

        if self._closed:
            raise self._closed

        try:
            await self._ctx.chan.send(
                payload=Yield(
                    cid=self._ctx.cid,
                    pld=data,
                ),
            )
        except (
            trio.ClosedResourceError,
            trio.BrokenResourceError,
            BrokenPipeError,
        ) as _trans_err:
            trans_err = _trans_err
            if (
                hide_tb
                and
                self._ctx.chan._exc is trans_err
                # ^XXX, IOW, only if the channel is marked errored
                # for the same reason as whatever its underlying
                # transport raised, do we keep the full low-level tb
                # suppressed from the user.
            ):
                raise type(trans_err)(
                    *trans_err.args
                ) from trans_err
            else:
                raise

    # TODO: msg capability context api1
    # @acm
    # async def enable_msg_caps(
    #     self,
    #     msg_subtypes: Union[
    #         list[list[Struct]],
    #         Protocol,   # hypothetical type that wraps a msg set
    #     ],
    # ) -> tuple[Callable, Callable]:  # payload enc, dec pair
    #     ...


@acm
async def open_stream_from_ctx(
    ctx: Context,
    allow_overruns: bool|None = False,
    msg_buffer_size: int|None = None,

) -> AsyncGenerator[MsgStream, None]:
    '''
    Open a `MsgStream`, a bi-directional msg transport dialog
    connected to the cross-actor peer task for an IPC `Context`.

    This context manager must be entered in both the "parent" (task
    which entered `Portal.open_context()`) and "child" (RPC task
    which is decorated by `@context`) tasks for the stream to
    logically be considered "open"; if one side begins sending to an
    un-opened peer, depending on policy config, msgs will either be
    queued until the other side opens and/or a `StreamOverrun` will
    (eventually) be raised.

                         ------ - ------

    Runtime semantics design:

    A `MsgStream` session adheres to "one-shot use" semantics,
    meaning if you close the scope it **can not** be "re-opened".

    Instead you must re-establish a new surrounding RPC `Context`
    (RTC: remote task context?) using `Portal.open_context()`.

    In the future this *design choice* may need to be changed but
    currently there seems to be no obvious reason to support such
    semantics..

    - "pausing a stream" can be supported with a message implemented
      by the `tractor` application dev.

    - any remote error will normally require a restart of the entire
      `trio.Task`'s scope due to the nature of `trio`'s cancellation
      (`CancelScope`) system and semantics (level triggered).

    '''
    actor: Actor = ctx._actor

    # If the surrounding context has been cancelled by some
    # task with a handle to THIS, we error here immediately
    # since it likely means the surrounding lexical-scope has
    # errored, been `trio.Cancelled` or at the least
    # `Context.cancel()` was called by some task.
    if ctx._cancel_called:

        # XXX NOTE: ALWAYS RAISE any remote error here even if
        # it's an expected `ContextCancelled` due to a local
        # task having called `.cancel()`!
        #
        # WHY: we expect the error to always bubble up to the
        # surrounding `Portal.open_context()` call and be
        # absorbed there (silently) and we DO NOT want to
        # actually try to stream - a cancel msg was already
        # sent to the other side!
        ctx.maybe_raise(
            raise_ctxc_from_self_call=True,
        )
        # NOTE: this is diff then calling
        # `._maybe_raise_remote_err()` specifically
        # because we want to raise a ctxc on any task entering this `.open_stream()`
        # AFTER cancellation was already been requested,
        # we DO NOT want to absorb any ctxc ACK silently!
        # if ctx._remote_error:
        #     raise ctx._remote_error

        # XXX NOTE: if no `ContextCancelled` has been responded
        # back from the other side (yet), we raise a different
        # runtime error indicating that this task's usage of
        # `Context.cancel()` and then `.open_stream()` is WRONG!
        task: str = trio.lowlevel.current_task().name
        raise RuntimeError(
            'Stream opened after `Context.cancel()` called..?\n'
            f'task: {actor.uid[0]}:{task}\n'
            f'{ctx}'
        )

    if (
        not ctx._portal
        and not ctx._started_called
    ):
        raise RuntimeError(
            'Context.started()` must be called before opening a stream'
        )

    # NOTE: in one way streaming this only happens on the
    # parent-ctx-task side (on the side that calls
    # `Actor.start_remote_task()`) so if you try to send
    # a stop from the caller to the callee in the
    # single-direction-stream case you'll get a lookup error
    # currently.
    ctx: Context = actor.get_context(
        chan=ctx.chan,
        cid=ctx.cid,
        nsf=ctx._nsf,
        # side=ctx.side,

        msg_buffer_size=msg_buffer_size,
        allow_overruns=allow_overruns,
    )
    ctx._allow_overruns: bool = allow_overruns
    assert ctx is ctx

    # XXX: If the underlying channel feeder receive mem chan has
    # been closed then likely client code has already exited
    # a ``.open_stream()`` block prior or there was some other
    # unanticipated error or cancellation from ``trio``.

    if ctx._rx_chan._closed:
        raise trio.ClosedResourceError(
            'The underlying channel for this stream was already closed!\n'
        )

    # NOTE: implicitly this will call `MsgStream.aclose()` on
    # `.__aexit__()` due to stream's parent `Channel` type!
    #
    # XXX NOTE XXX: ensures the stream is "one-shot use",
    # which specifically means that on exit,
    # - signal ``trio.EndOfChannel``/``StopAsyncIteration`` to
    #   the far end indicating that the caller exited
    #   the streaming context purposefully by letting
    #   the exit block exec.
    # - this is diff from the cancel/error case where
    #   a cancel request from this side or an error
    #   should be sent to the far end indicating the
    #   stream WAS NOT just closed normally/gracefully.
    async with MsgStream(
        ctx=ctx,
        rx_chan=ctx._rx_chan,
    ) as stream:

        # NOTE: we track all existing streams per portal for
        # the purposes of attempting graceful closes on runtime
        # cancel requests.
        if ctx._portal:
            ctx._portal._streams.add(stream)

        try:
            ctx._stream_opened: bool = True
            ctx._stream = stream

            # XXX: do we need this?
            # ensure we aren't cancelled before yielding the stream
            # await trio.lowlevel.checkpoint()
            yield stream

            # XXX: (MEGA IMPORTANT) if this is a root opened process we
            # wait for any immediate child in debug before popping the
            # context from the runtime msg loop otherwise inside
            # ``Actor._deliver_ctx_payload()`` the msg will be discarded and in
            # the case where that msg is global debugger unlock (via
            # a "stop" msg for a stream), this can result in a deadlock
            # where the root is waiting on the lock to clear but the
            # child has already cleared it and clobbered IPC.
            #
            # await maybe_wait_for_debugger()

            # XXX TODO: pretty sure this isn't needed (see
            # note above this block) AND will result in
            # a double `.send_stop()` call. The only reason to
            # put it here would be to due with "order" in
            # terms of raising any remote error (as per
            # directly below) or bc the stream's
            # `.__aexit__()` block might not get run
            # (doubtful)? Either way if we did put this back
            # in we also need a state var to avoid the double
            # stop-msg send..
            #
            # await stream.aclose()

        # NOTE: absorb and do not raise any
        # EoC received from the other side such that
        # it is not raised inside the surrounding
        # context block's scope!
        except trio.EndOfChannel as eoc:
            if (
                eoc
                and
                stream.closed
            ):
                # sanity, can remove?
                assert eoc is stream._eoc

                log.runtime(
                    'Stream was terminated by EoC\n\n'
                    # NOTE: won't show the error <Type> but
                    # does show txt followed by IPC msg.
                    f'{str(eoc)}\n'
                )
        finally:
            if ctx._portal:
                try:
                    ctx._portal._streams.remove(stream)
                except KeyError:
                    log.warning(
                        f'Stream was already destroyed?\n'
                        f'actor: {ctx.chan.uid}\n'
                        f'ctx id: {ctx.cid}'
                    )



def stream(func: Callable) -> Callable:
    '''
    Mark an async function as a streaming routine with ``@stream``.

    '''
    # TODO: apply whatever solution ``mypy`` ends up picking for this:
    # https://github.com/python/mypy/issues/2087#issuecomment-769266912
    func._tractor_stream_function: bool = True  # type: ignore

    sig = inspect.signature(func)
    params = sig.parameters
    if 'stream' not in params and 'ctx' in params:
        warnings.warn(
            "`@tractor.stream decorated funcs should now declare a `stream` "
            " arg, `ctx` is now designated for use with @tractor.context",
            DeprecationWarning,
            stacklevel=2,
        )

    if (
        'ctx' not in params and
        'to_trio' not in params and
        'stream' not in params
    ):
        raise TypeError(
            "The first argument to the stream function "
            f"{func.__name__} must be `ctx: tractor.Context` "
            "(Or ``to_trio`` if using ``asyncio`` in guest mode)."
        )
    return func
