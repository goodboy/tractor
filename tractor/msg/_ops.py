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
Near-application abstractions for `MsgType.pld: PayloadT|Raw`
delivery, filtering and type checking as well as generic
operational helpers for processing transaction flows.

'''
from __future__ import annotations
from contextlib import (
    asynccontextmanager as acm,
    contextmanager as cm,
)
from typing import (
    Any,
    Type,
    TYPE_CHECKING,
    Union,
)
# ------ - ------
from msgspec import (
    msgpack,
    Raw,
    Struct,
    ValidationError,
)
import trio
# ------ - ------
from tractor.log import get_logger
from tractor._exceptions import (
    MessagingError,
    InternalError,
    _raise_from_unexpected_msg,
    MsgTypeError,
    _mk_msg_type_err,
    pack_error,
)
from tractor._state import current_ipc_ctx
from ._codec import (
    mk_dec,
    MsgDec,
    MsgCodec,
    current_codec,
)
from .types import (
    CancelAck,
    Error,
    MsgType,
    PayloadT,
    Return,
    Started,
    Stop,
    Yield,
    pretty_struct,
)


if TYPE_CHECKING:
    from tractor._context import Context
    from tractor._streaming import MsgStream


log = get_logger(__name__)


_def_any_pldec: MsgDec[Any] = mk_dec()


class PldRx(Struct):
    '''
    A "msg payload receiver".

    The pairing of a "feeder" `trio.abc.ReceiveChannel` and an
    interchange-specific (eg. msgpack) payload field decoder. The
    validation/type-filtering rules are runtime mutable and allow
    type constraining the set of `MsgType.pld: Raw|PayloadT`
    values at runtime, per IPC task-context.

    This abstraction, being just below "user application code",
    allows for the equivalent of our `MsgCodec` (used for
    typer-filtering IPC dialog protocol msgs against a msg-spec)
    but with granular control around payload delivery (i.e. the
    data-values user code actually sees and uses (the blobs that
    are "shuttled" by the wrapping dialog prot) such that invalid
    `.pld: Raw` can be decoded and handled by IPC-primitive user
    code (i.e. that operates on `Context` and `Msgstream` APIs)
    without knowledge of the lower level `Channel`/`MsgTransport`
    primitives nor the `MsgCodec` in use. Further, lazily decoding
    payload blobs allows for topical (and maybe intentionally
    "partial") encryption of msg field subsets.

    '''
    # TODO: better to bind it here?
    # _rx_mc: trio.MemoryReceiveChannel
    _pld_dec: MsgDec
    _ctx: Context|None = None
    _ipc: Context|MsgStream|None = None

    @property
    def pld_dec(self) -> MsgDec:
        return self._pld_dec

    # TODO: a better name?
    # -[ ] when would this be used as it avoids needingn to pass the
    #   ipc prim to every method
    @cm
    def wraps_ipc(
        self,
        ipc_prim: Context|MsgStream,

    ) -> PldRx:
        '''
        Apply this payload receiver to an IPC primitive type, one
        of `Context` or `MsgStream`.

        '''
        self._ipc = ipc_prim
        try:
            yield self
        finally:
            self._ipc = None

    @cm
    def limit_plds(
        self,
        spec: Union[Type[Struct]],

    ) -> MsgDec:
        '''
        Type-limit the loadable msg payloads via an applied
        `MsgDec` given an input spec, revert to prior decoder on
        exit.

        '''
        orig_dec: MsgDec = self._pld_dec
        limit_dec: MsgDec = mk_dec(spec=spec)
        try:
            self._pld_dec = limit_dec
            yield limit_dec
        finally:
            self._pld_dec = orig_dec

    @property
    def dec(self) -> msgpack.Decoder:
        return self._pld_dec.dec

    def recv_pld_nowait(
        self,
        # TODO: make this `MsgStream` compat as well, see above^
        # ipc_prim: Context|MsgStream,
        ipc: Context|MsgStream,

        ipc_msg: MsgType|None = None,
        expect_msg: Type[MsgType]|None = None,
        hide_tb: bool = False,
        **dec_msg_kwargs,

    ) -> Any|Raw:
        __tracebackhide__: bool = hide_tb

        msg: MsgType = (
            ipc_msg
            or

            # sync-rx msg from underlying IPC feeder (mem-)chan
            ipc._rx_chan.receive_nowait()
        )
        return self.dec_msg(
            msg,
            ipc=ipc,
            expect_msg=expect_msg,
            hide_tb=hide_tb,
            **dec_msg_kwargs,
        )

    async def recv_pld(
        self,
        ipc: Context|MsgStream,
        ipc_msg: MsgType|None = None,
        expect_msg: Type[MsgType]|None = None,
        hide_tb: bool = True,

        **dec_msg_kwargs,

    ) -> Any|Raw:
        '''
        Receive a `MsgType`, then decode and return its `.pld` field.

        '''
        __tracebackhide__: bool = hide_tb
        msg: MsgType = (
            ipc_msg
            or
            # async-rx msg from underlying IPC feeder (mem-)chan
            await ipc._rx_chan.receive()
        )
        return self.dec_msg(
            msg=msg,
            ipc=ipc,
            expect_msg=expect_msg,
            **dec_msg_kwargs,
        )

    # TODO: rename to,
    # -[ ] `.decode_pld()`?
    # -[ ] `.dec_pld()`?
    def dec_msg(
        self,
        msg: MsgType,
        ipc: Context|MsgStream,
        expect_msg: Type[MsgType]|None,

        raise_error: bool = True,
        hide_tb: bool = True,

        # XXX for special (default?) case of send side call with
        # `Context.started(validate_pld_spec=True)`
        is_started_send_side: bool = False,

    ) -> PayloadT|Raw:
        '''
        Decode a msg's payload field: `MsgType.pld: PayloadT|Raw` and
        return the value or raise an appropriate error.

        '''
        __tracebackhide__: bool = hide_tb
        src_err: BaseException|None = None
        match msg:
            # payload-data shuttle msg; deliver the `.pld` value
            # directly to IPC (primitive) client-consumer code.
            case (
                Started(pld=pld)  # sync phase
                |Yield(pld=pld)  # streaming phase
                |Return(pld=pld)  # termination phase
            ):
                try:
                    pld: PayloadT = self._pld_dec.decode(pld)
                    log.runtime(
                        'Decoded msg payload\n\n'
                        f'{msg}\n'
                        f'where payload decoded as\n'
                        f'|_pld={pld!r}\n'
                    )
                    return pld

                # XXX pld-value type failure
                except ValidationError as valerr:
                    # pack mgterr into error-msg for
                    # reraise below; ensure remote-actor-err
                    # info is displayed nicely?
                    mte: MsgTypeError = _mk_msg_type_err(
                        msg=msg,
                        codec=self.pld_dec,
                        src_validation_error=valerr,
                        is_invalid_payload=True,
                        expected_msg=expect_msg,
                    )
                    err_msg: Error = pack_error(
                        exc=mte,
                        cid=msg.cid,
                        src_uid=(
                            ipc.chan.uid
                            if not is_started_send_side
                            else ipc._actor.uid
                        ),
                        # tb=valerr.__traceback__,
                        tb_str=mte._message,
                    )
                    mte._ipc_msg = err_msg

                    # NOTE: just raise the MTE inline instead of all
                    # the pack-unpack-repack non-sense when this is
                    # a "send side" validation error.
                    if is_started_send_side:
                        raise mte

                        # XXX TODO: remove this right?
                        # => any bad stated/return values should
                        # always be treated a remote errors right?
                        #
                        # if (
                        #     expect_msg is Return
                        #     or expect_msg is Started
                        # ):
                        #     # set emulated remote error more-or-less as the
                        #     # runtime would
                        #     ctx: Context = getattr(ipc, 'ctx', ipc)
                        #     ctx._maybe_cancel_and_set_remote_error(mte)


                    # XXX override the `msg` passed to
                    # `_raise_from_unexpected_msg()` (below) so so
                    # that we're effectively able to use that same
                    # func to unpack and raise an "emulated remote
                    # `Error`" of this local MTE.
                    msg = err_msg
                    # XXX NOTE: so when the `_raise_from_unexpected_msg()`
                    # raises the boxed `err_msg` from above it raises
                    # it from the above caught interchange-lib
                    # validation error.
                    src_err = valerr

                    # TODO: should we instead make this explicit and
                    # use the above masked `is_started_send_decode`,
                    # expecting the `Context.started()` caller to set
                    # it? Rn this is kinda, howyousayyy, implicitly
                    # edge-case-y..
                    # TODO: remove this since it's been added to
                    # `_raise_from_unexpected_msg()`..?
                    # if (
                    #     expect_msg is not Started
                    #     and not is_started_send_side
                    # ):
                    #     # set emulated remote error more-or-less as the
                    #     # runtime would
                    #     ctx: Context = getattr(ipc, 'ctx', ipc)
                    #     ctx._maybe_cancel_and_set_remote_error(mte)

                # XXX some other decoder specific failure?
                # except TypeError as src_error:
                #     from .devx import mk_pdb
                #     mk_pdb().set_trace()
                #     raise src_error
                # ^-TODO-^ can remove?

            # a runtime-internal RPC endpoint response.
            # always passthrough since (internal) runtime
            # responses are generally never exposed to consumer
            # code.
            case CancelAck(
                pld=bool(cancelled)
            ):
                return cancelled

            case Error():
                src_err = MessagingError(
                    'IPC ctx dialog terminated without `Return`-ing a result\n'
                    f'Instead it raised {msg.boxed_type_str!r}!'
                )
                # XXX NOTE XXX another super subtle runtime-y thing..
                #
                # - when user code (transitively) calls into this
                #   func (usually via a `Context/MsgStream` API) we
                #   generally want errors to propagate immediately
                #   and directly so that the user can define how it
                #   wants to handle them.
                #
                #  HOWEVER,
                #
                # - for certain runtime calling cases, we don't want to
                #   directly raise since the calling code might have
                #   special logic around whether to raise the error
                #   or supress it silently (eg. a `ContextCancelled`
                #   received from the far end which was requested by
                #   this side, aka a self-cancel).
                #
                # SO, we offer a flag to control this.
                if not raise_error:
                    return src_err

            case Stop(cid=cid):
                ctx: Context = getattr(ipc, 'ctx', ipc)
                message: str = (
                    f'{ctx.side!r}-side of ctx received stream-`Stop` from '
                    f'{ctx.peer_side!r} peer ?\n'
                    f'|_cid: {cid}\n\n'

                    f'{pretty_struct.pformat(msg)}\n'
                )
                if ctx._stream is None:
                    explain: str = (
                        f'BUT, no `MsgStream` (was) open(ed) on this '
                        f'{ctx.side!r}-side of the IPC ctx?\n'
                        f'Maybe check your code for streaming phase race conditions?\n'
                    )
                    log.warning(
                        message
                        +
                        explain
                    )
                    # let caller decide what to do when only one
                    # side opened a stream, don't raise.
                    return msg

                else:
                    explain: str = (
                        'Received a `Stop` when it should NEVER be possible!?!?\n'
                    )
                    # TODO: this is constructed inside
                    # `_raise_from_unexpected_msg()` but maybe we
                    # should pass it in?
                    # src_err = trio.EndOfChannel(explain)
                    src_err = None

            case _:
                src_err = InternalError(
                    'Unknown IPC msg ??\n\n'
                    f'{msg}\n'
                )

        # TODO: maybe use the new `.add_note()` from 3.11?
        # |_https://docs.python.org/3.11/library/exceptions.html#BaseException.add_note
        #
        # fallthrough and raise from `src_err`
        try:
            _raise_from_unexpected_msg(
                ctx=getattr(ipc, 'ctx', ipc),
                msg=msg,
                src_err=src_err,
                log=log,
                expect_msg=expect_msg,
                hide_tb=hide_tb,
            )
        except UnboundLocalError:
            # XXX if there's an internal lookup error in the above
            # code (prolly on `src_err`) we want to show this frame
            # in the tb!
            __tracebackhide__: bool = False
            raise

    async def recv_msg_w_pld(
        self,
        ipc: Context|MsgStream,
        expect_msg: MsgType,

        # NOTE: generally speaking only for handling `Stop`-msgs that
        # arrive during a call to `drain_to_final_msg()` above!
        passthrough_non_pld_msgs: bool = True,
        hide_tb: bool = True,
        **kwargs,

    ) -> tuple[MsgType, PayloadT]:
        '''
        Retrieve the next avail IPC msg, decode it's payload, and return
        the pair of refs.

        '''
        __tracebackhide__: bool = hide_tb
        msg: MsgType = await ipc._rx_chan.receive()

        if passthrough_non_pld_msgs:
            match msg:
                case Stop():
                    return msg, None

        # TODO: is there some way we can inject the decoded
        # payload into an existing output buffer for the original
        # msg instance?
        pld: PayloadT = self.dec_msg(
            msg,
            ipc=ipc,
            expect_msg=expect_msg,
            hide_tb=hide_tb,
            **kwargs,
        )
        return msg, pld


@cm
def limit_plds(
    spec: Union[Type[Struct]],
    **kwargs,

) -> MsgDec:
    '''
    Apply a `MsgCodec` that will natively decode the SC-msg set's
    `PayloadMsg.pld: Union[Type[Struct]]` payload fields using
    tagged-unions of `msgspec.Struct`s from the `payload_types`
    for all IPC contexts in use by the current `trio.Task`.

    '''
    __tracebackhide__: bool = True
    try:
        curr_ctx: Context = current_ipc_ctx()
        rx: PldRx = curr_ctx._pld_rx
        orig_pldec: MsgDec = rx.pld_dec

        with rx.limit_plds(
            spec=spec,
            **kwargs,
        ) as pldec:
            log.runtime(
                'Applying payload-decoder\n\n'
                f'{pldec}\n'
            )
            yield pldec
    finally:
        log.runtime(
            'Reverted to previous payload-decoder\n\n'
            f'{orig_pldec}\n'
        )
        # sanity on orig settings
        assert rx.pld_dec is orig_pldec


@acm
async def maybe_limit_plds(
    ctx: Context,
    spec: Union[Type[Struct]]|None = None,
    **kwargs,
) -> MsgDec|None:
    '''
    Async compat maybe-payload type limiter.

    Mostly for use inside other internal `@acm`s such that a separate
    indent block isn't needed when an async one is already being
    used.

    '''
    if spec is None:
        yield None
        return

    # sanity on scoping
    curr_ctx: Context = current_ipc_ctx()
    assert ctx is curr_ctx

    with ctx._pld_rx.limit_plds(spec=spec) as msgdec:
        yield msgdec

    curr_ctx: Context = current_ipc_ctx()
    assert ctx is curr_ctx


async def drain_to_final_msg(
    ctx: Context,

    hide_tb: bool = True,
    msg_limit: int = 6,

) -> tuple[
    Return|None,
    list[MsgType]
]:
    '''
    Drain IPC msgs delivered to the underlying IPC primitive's
    rx-mem-chan (eg. `Context._rx_chan`) from the runtime in
    search for a final result or error.

    The motivation here is to ideally capture errors during ctxc
    conditions where a canc-request/or local error is sent but the
    local task also excepts and enters the
    `Portal.open_context().__aexit__()` block wherein we prefer to
    capture and raise any remote error or ctxc-ack as part of the
    `ctx.result()` cleanup and teardown sequence.

    '''
    __tracebackhide__: bool = hide_tb
    raise_overrun: bool = not ctx._allow_overruns

    # wait for a final context result by collecting (but
    # basically ignoring) any bi-dir-stream msgs still in transit
    # from the far end.
    pre_result_drained: list[MsgType] = []
    result_msg: Return|Error|None = None
    while not (
        ctx.maybe_error
        and not ctx._final_result_is_set()
    ):
        try:
            # receive all msgs, scanning for either a final result
            # or error; the underlying call should never raise any
            # remote error directly!
            msg, pld = await ctx._pld_rx.recv_msg_w_pld(
                ipc=ctx,
                expect_msg=Return,
                raise_error=False,
                hide_tb=hide_tb,
            )
            # ^-TODO-^ some bad ideas?
            # -[ ] wrap final outcome .receive() in a scope so
            #     it can be cancelled out of band if needed?
            # |_with trio.CancelScope() as res_cs:
            #       ctx._res_scope = res_cs
            #       msg: dict = await ctx._rx_chan.receive()
            #   if res_cs.cancelled_caught:
            #
            # -[ ] make sure pause points work here for REPLing
            #   the runtime itself; i.e. ensure there's no hangs!
            # |_from tractor.devx._debug import pause
            #   await pause()


        # NOTE: we get here if the far end was
        # `ContextCancelled` in 2 cases:
        # 1. we requested the cancellation and thus
        #    SHOULD NOT raise that far end error,
        # 2. WE DID NOT REQUEST that cancel and thus
        #    SHOULD RAISE HERE!
        except trio.Cancelled as taskc:

            # CASE 2: mask the local cancelled-error(s)
            # only when we are sure the remote error is
            # the source cause of this local task's
            # cancellation.
            ctx.maybe_raise()

            # CASE 1: we DID request the cancel we simply
            # continue to bubble up as normal.
            raise taskc

        match msg:

            # final result arrived!
            case Return():
                log.runtime(
                    'Context delivered final draining msg:\n'
                    f'{pretty_struct.pformat(msg)}'
                )
                ctx._result: Any = pld
                result_msg = msg
                break

            # far end task is still streaming to us so discard
            # and report depending on local ctx state.
            case Yield():
                pre_result_drained.append(msg)
                if (
                    (ctx._stream.closed
                     and (reason := 'stream was already closed')
                    )
                    or (ctx.cancel_acked
                        and (reason := 'ctx cancelled other side')
                    )
                    or (ctx._cancel_called
                        and (reason := 'ctx called `.cancel()`')
                    )
                    or (len(pre_result_drained) > msg_limit
                        and (reason := f'"yield" limit={msg_limit}')
                    )
                ):
                    log.cancel(
                        'Cancelling `MsgStream` drain since '
                        f'{reason}\n\n'
                        f'<= {ctx.chan.uid}\n'
                        f'  |_{ctx._nsf}()\n\n'
                        f'=> {ctx._task}\n'
                        f'  |_{ctx._stream}\n\n'

                        f'{pretty_struct.pformat(msg)}\n'
                    )
                    break

                # drain up to the `msg_limit` hoping to get
                # a final result or error/ctxc.
                else:
                    log.warning(
                        'Ignoring "yield" msg during `ctx.result()` drain..\n'
                        f'<= {ctx.chan.uid}\n'
                        f'  |_{ctx._nsf}()\n\n'
                        f'=> {ctx._task}\n'
                        f'  |_{ctx._stream}\n\n'

                        f'{pretty_struct.pformat(msg)}\n'
                    )
                    continue

            # stream terminated, but no result yet..
            #
            # TODO: work out edge cases here where
            # a stream is open but the task also calls
            # this?
            # -[ ] should be a runtime error if a stream is open right?
            # Stop()
            case Stop():
                pre_result_drained.append(msg)
                log.cancel(
                    'Remote stream terminated due to "stop" msg:\n\n'
                    f'{pretty_struct.pformat(msg)}\n'
                )
                continue

            # remote error msg, likely already handled inside
            # `Context._deliver_msg()`
            case Error():
                # TODO: can we replace this with `ctx.maybe_raise()`?
                # -[ ]  would this be handier for this case maybe?
                # |_async with maybe_raise_on_exit() as raises:
                #       if raises:
                #           log.error('some msg about raising..')
                #
                re: Exception|None = ctx._remote_error
                if re:
                    assert msg is ctx._cancel_msg
                    # NOTE: this solved a super duper edge case XD
                    # this was THE super duper edge case of:
                    # - local task opens a remote task,
                    # - requests remote cancellation of far end
                    #   ctx/tasks,
                    # - needs to wait for the cancel ack msg
                    #   (ctxc) or some result in the race case
                    #   where the other side's task returns
                    #   before the cancel request msg is ever
                    #   rxed and processed,
                    # - here this surrounding drain loop (which
                    #   iterates all ipc msgs until the ack or
                    #   an early result arrives) was NOT exiting
                    #   since we are the edge case: local task
                    #   does not re-raise any ctxc it receives
                    #   IFF **it** was the cancellation
                    #   requester..
                    #
                    # XXX will raise if necessary but ow break
                    # from loop presuming any supressed error
                    # (ctxc) should terminate the context!
                    ctx._maybe_raise_remote_err(
                        re,
                        # NOTE: obvi we don't care if we
                        # overran the far end if we're already
                        # waiting on a final result (msg).
                        # raise_overrun_from_self=False,
                        raise_overrun_from_self=raise_overrun,
                    )
                    result_msg = msg
                    break  # OOOOOF, yeah obvi we need this..

                else:
                    # bubble the original src key error
                    raise

            # XXX should pretty much never get here unless someone
            # overrides the default `MsgType` spec.
            case _:
                pre_result_drained.append(msg)
                # It's definitely an internal error if any other
                # msg type without a`'cid'` field arrives here!
                if not msg.cid:
                    raise InternalError(
                        'Unexpected cid-missing msg?\n\n'
                        f'{msg}\n'
                    )

                raise RuntimeError('Unknown msg type: {msg}')

    else:
        log.cancel(
            'Skipping `MsgStream` drain since final outcome is set\n\n'
            f'{ctx.outcome}\n'
        )

    return (
        result_msg,
        pre_result_drained,
    )


def validate_payload_msg(
    pld_msg: Started|Yield|Return,
    pld_value: PayloadT,
    ipc: Context|MsgStream,

    raise_mte: bool = True,
    strict_pld_parity: bool = False,
    hide_tb: bool = True,

) -> MsgTypeError|None:
    '''
    Validate a `PayloadMsg.pld` value with the current
    IPC ctx's `PldRx` and raise an appropriate `MsgTypeError`
    on failure.

    '''
    __tracebackhide__: bool = hide_tb
    codec: MsgCodec = current_codec()
    msg_bytes: bytes = codec.encode(pld_msg)
    try:
        roundtripped: Started = codec.decode(msg_bytes)
        ctx: Context = getattr(ipc, 'ctx', ipc)
        pld: PayloadT = ctx.pld_rx.dec_msg(
            msg=roundtripped,
            ipc=ipc,
            expect_msg=Started,
            hide_tb=hide_tb,
            is_started_send_side=True,
        )
        if (
            strict_pld_parity
            and
            pld != pld_value
        ):
            # TODO: make that one a mod func too..
            diff = pretty_struct.Struct.__sub__(
                roundtripped,
                pld_msg,
            )
            complaint: str = (
                'Started value does not match after roundtrip?\n\n'
                f'{diff}'
            )
            raise ValidationError(complaint)

    # raise any msg type error NO MATTER WHAT!
    except ValidationError as verr:
        mte: MsgTypeError = _mk_msg_type_err(
            msg=roundtripped,
            codec=codec,
            src_validation_error=verr,
            verb_header='Trying to send ',
            is_invalid_payload=True,
        )
        if not raise_mte:
            return mte

        raise mte from verr
