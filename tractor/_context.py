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
The fundamental cross-process SC abstraction: an inter-actor,
transitively cancel-scope linked, (dual) task IPC coupled "context".

A `Context` is very similar to the look and feel of the
`.cancel_scope: trio.CancelScope` built into each `trio.Nursery`
except that it links the lifetimes of 2 memory space disjoint,
parallel executing, tasks scheduled in separate "actors".

So while a `trio.Nursery` has a `.parent_task` which exists both
before (open) and then inside the body of the `async with` of the
nursery's scope (/block), a `Context` contains 2 tasks, a "parent"
and a "child" side, where both execute independently in separate
memory domains of different (host's) processes linked through
a SC-transitive IPC "shuttle dialog protocol". The underlying IPC
dialog-(un)protocol allows for the maintainance of SC properties
end-2-end between the tasks.

'''
from __future__ import annotations
from collections import deque
from contextlib import (
    asynccontextmanager as acm,
)
from contextvars import Token
from dataclasses import (
    dataclass,
    field,
)
from functools import partial
import inspect
from pprint import pformat
import textwrap
from types import (
    UnionType,
)
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Type,
    TypeAlias,
    TYPE_CHECKING,
    Union,
)
import warnings
# ------ - ------
import trio
from trio.lowlevel import Task
# ------ - ------
from ._exceptions import (
    ContextCancelled,
    InternalError,
    MsgTypeError,
    RemoteActorError,
    StreamOverrun,
    pack_from_raise,
    unpack_error,
)
from .log import (
    get_logger,
    at_least_level,
)
from .msg import (
    Error,
    MsgType,
    NamespacePath,
    PayloadT,
    Return,
    Started,
    Stop,
    Yield,
    pretty_struct,
    _ops as msgops,
)
from .ipc import (
    Channel,
)
from ._streaming import (
    MsgStream,
    open_stream_from_ctx,
)
from ._state import (
    current_actor,
    debug_mode,
    _ctxvar_Context,
)
# ------ - ------
if TYPE_CHECKING:
    from ._portal import Portal
    from ._runtime import Actor
    from .ipc._transport import MsgTransport
    from .devx._frame_stack import (
        CallerInfo,
    )

log = get_logger(__name__)


class Unresolved:
    '''
    Placeholder value for `Context._result` until
    a final return value or raised error is resolved.

    '''


# TODO: make this a .msg.types.Struct!
# -[ ] ideally we can freeze it
# -[ ] let's us do field diffing nicely in tests Bo
@dataclass
class Context:
    '''
    An inter-actor, SC transitive, `trio.Task` (pair)
    communication context.

    (We've also considered other names and ideas:
     - "communicating tasks scope": cts
     - "distributed task scope": dts
     - "communicating tasks context": ctc

     **Got a better idea for naming? Make an issue dawg!**
    )

    NB: This class should **never be instatiated directly**, it is
    allocated by the runtime in 2 ways:
     - by entering `Portal.open_context()` which is the primary
       public API for any "parent" task or,
     - by the RPC machinery's `._rpc._invoke()` as a `ctx` arg
       to a remotely scheduled "child" function.

    AND is always constructed using the below `mk_context()`.

    Allows maintaining task or protocol specific state between
    2 cancel-scope-linked, communicating and parallel executing
    `Task`s. Contexts are allocated on each side of any task
    RPC-linked msg dialog, i.e. for every request to a remote
    actor from a `Portal`. On the "callee" side a context is
    always allocated inside `._rpc._invoke()`.

    TODO: more detailed writeup on cancellation, error and
    streaming semantics..

    A context can be cancelled and (possibly eventually restarted) from
    either side of the underlying IPC channel, it can also open task
    oriented message streams,  and acts more or less as an IPC aware
    inter-actor-task ``trio.CancelScope``.

    '''
    chan: Channel
    cid: str  # "context id", more or less a unique linked-task-pair id

    _actor: Actor

    # the "feeder" channels for delivering message values to the
    # local task from the runtime's msg processing loop.
    _rx_chan: trio.MemoryReceiveChannel
    _send_chan: trio.MemorySendChannel

    # payload receiver
    _pld_rx: msgops.PldRx

    @property
    def pld_rx(self) -> msgops.PldRx:
        '''
        The current `tractor.Context`'s msg-payload-receiver.

        A payload receiver is the IPC-msg processing sub-sys which
        filters inter-actor-task communicated payload data, i.e. the
        `PayloadMsg.pld: PayloadT` field value, AFTER its container
        shuttlle msg (eg. `Started`/`Yield`/`Return) has been
        delivered up from `tractor`'s transport layer but BEFORE the
        data is yielded to `tractor` application code.

        The "IPC-primitive API" is normally one of a `Context` (this)` or a `MsgStream`
        or some higher level API using one of them.

        For ex. `pld_data: PayloadT = MsgStream.receive()` implicitly
        calls into the stream's parent `Context.pld_rx.recv_pld().` to
        receive the latest `PayloadMsg.pld` value.

        Modification of the current payload spec via `limit_plds()`
        allows a `tractor` application to contextually filter IPC
        payload content with a type specification as supported by the
        interchange backend.

        - for `msgspec` see <PUTLINKHERE>.

        Note that the `PldRx` itself is a per-`Context` instance that
        normally only changes when some (sub-)task, on a given "side"
        of the IPC ctx (either a "child"-side RPC or inside
        a "parent"-side `Portal.open_context()` block), modifies it
        using the `.msg._ops.limit_plds()` API.

        '''
        return self._pld_rx

    # full "namespace-path" to target RPC function
    _nsf: NamespacePath

    # the "invocation type" of the far end task-entry-point
    # function, normally matching a logic block inside
    # `._runtime.invoke()`.
    _remote_func_type: str | None = None

    # NOTE: (for now) only set (a portal) on the caller side since
    # the callee doesn't generally need a ref to one and should
    # normally need to explicitly ask for handle to its peer if
    # more the the `Context` is needed?
    _portal: Portal | None = None

    @property
    def portal(self) -> Portal|None:
        '''
        Return any wrapping memory-`Portal` if this is
        a 'parent'-side task which called `Portal.open_context()`,
        otherwise `None`.

        '''
        return self._portal

    # NOTE: each side of the context has its own cancel scope
    # which is exactly the primitive that allows for
    # cross-actor-task-supervision and thus SC.
    _scope: trio.CancelScope|None = None
    _task: Task|None = None

    # TODO: cs around result waiting so we can cancel any
    # permanently blocking `._rx_chan.receive()` call in
    # a drain loop?
    # _res_scope: trio.CancelScope|None = None

    _outcome_msg: Return|Error|ContextCancelled = Unresolved

    # on a clean exit there should be a final value
    # delivered from the far end "callee" task, so
    # this value is only set on one side.
    # _result: Any | int = None
    _result: PayloadT|Unresolved = Unresolved

    # if the local "caller"  task errors this value is always set
    # to the error that was captured in the
    # `Portal.open_context().__aexit__()` teardown block OR, in
    # 2 special cases when an (maybe) expected remote error
    #   arrives that we purposely swallow silently:
    # - `ContextCancelled` with `.canceller` set to our uid:
    #    a self-cancel,
    # - `RemoteActorError[StreamOverrun]` which was caught during
    #   a self-cancellation teardown msg drain.
    _local_error: BaseException|None = None

    # if the either side gets an error from the other
    # this value is set to that error unpacked from an
    # IPC msg.
    _remote_error: BaseException|None = None

    # only set if an actor-local task called `.cancel()`
    _cancel_called: bool = False  # did WE request cancel of the far end?

    # TODO: do we even need this? we can assume that if we're
    # cancelled that the other side is as well, so maybe we should
    # instead just have a `.canceller` pulled from the
    # `ContextCancelled`?
    _canceller: tuple[str, str]|None = None

    # NOTE: we try to ensure assignment of a "cancel msg" since
    # there's always going to be an "underlying reason" that any
    # context was closed due to either a remote side error or
    # a call to `.cancel()` which triggers `ContextCancelled`.
    _cancel_msg: str|dict|None = None

    # NOTE: this state-var is used by the runtime to determine if the
    # `pdbp` REPL is allowed to engage on contexts terminated via
    # a `ContextCancelled` due to a call to `.cancel()` triggering
    # "graceful closure" on either side:
    # - `._runtime._invoke()` will check this flag before engaging
    #   the crash handler REPL in such cases where the "callee"
    #   raises the cancellation,
    # - `.devx._debug.lock_stdio_for_peer()` will set it to `False` if
    #   the global tty-lock has been configured to filter out some
    #   actors from being able to acquire the debugger lock.
    _enter_debugger_on_cancel: bool = True

    # init and streaming state
    _started_called: bool = False
    _started_msg: MsgType|None = None
    _started_pld: Any = None
    _stream_opened: bool = False
    _stream: MsgStream|None = None

    # caller of `Portal.open_context()` for
    # logging purposes mostly
    _caller_info: CallerInfo|None = None

    # overrun handling machinery
    # NOTE: none of this provides "backpressure" to the remote
    # task, only an ability to not lose messages when the local
    # task is configured to NOT transmit ``StreamOverrun``s back
    # to the other side.
    _overflow_q: deque[dict] = field(
        default_factory=partial(
            deque,
            maxlen=616,
        )
    )

    # NOTE: this was originally a legacy interface from when we
    # were raising remote errors (set to `._remote_error`) by
    # starting a task inside this nursery that simply raised the
    # boxed exception. NOW, it's used for spawning overrun queuing
    # tasks when `.allow_overruns ==  True` !!!
    _scope_nursery: trio.Nursery|None = None
    # ^-TODO-^ change name?
    # -> `._scope_tn` "scope task nursery"

    # streaming overrun state tracking
    _in_overrun: bool = False
    _allow_overruns: bool = False

    # TODO: figure out how we can enforce this without losing our minds..
    _strict_started: bool = False
    _cancel_on_msgerr: bool = True

    def pformat(
        self,
        extra_fields: dict[str, Any]|None = None,
        # ^-TODO-^ some built-in extra state fields
        # we'll want in some devx specific cases?
        indent: str|None = None,

    ) -> str:
        ds: str = '='
        # ds: str = ': '

        # only show if opened
        maybe_stream_repr: str = ''
        if stream := self._stream:
            # TODO: a `MsgStream.reprol()` !!
            # f'   stream{ds}{self._stream}\n'
            # f'   {self._stream}\n'
            maybe_stream_repr: str = (
                f'   {stream}\n'
            )

        outcome_str: str = self.repr_outcome(
            show_error_fields=True
        )
        fmtstr: str = (
            # f'\n'
            # f'   ---\n'
            f' |_ipc: {self.dst_maddr}\n'
            # f'   dst_maddr{ds}{self.dst_maddr}\n'
            f"   uid{ds}'{self.chan.aid}'\n"
            f"   cid{ds}'{self.cid}'\n"
            # f'   ---\n'
            f'\n'
            # f'   ---\n'
            f' |_rpc: {self.repr_rpc}\n'
            f"   side{ds}'{self.side}'\n"
            # f'   rpc_sig{ds}{self.repr_rpc}\n'
            f'   {self._task}\n'
            f'{maybe_stream_repr}'
            # f'   ---\n'
            f'\n'
            # f'   -----\n'
            #
            # TODO: better state `str`ids?
            # -[x] maybe map err-types to strs like 'cancelled',
            #     'errored', 'streaming', 'started', .. etc.
            # -[ ] as well as a final result wrapper like
            #     `outcome.Value`?
            #
            f' |_state: {self.repr_state!r}\n'

            f'   outcome{ds}{outcome_str}\n'
            f'   result{ds}{self._result}\n'
            f'   cancel_called{ds}{self.cancel_called}\n'
            f'   cancel_acked{ds}{self.cancel_acked}\n'
            f'   canceller{ds}{self._canceller}\n'

            # TODO: any other fields?
            # f'   maybe_error{ds}{self.maybe_error}\n'
            # -[ ] ^ already covered by outcome yah?
            # f'  cancelled_caught={self.cancelled_caught}\n'
            # -[ ] remove this ^ right?

            # f'  _remote_error={self._remote_error}
        )
        if extra_fields:
            for key, val in extra_fields.items():
                fmtstr += (
                    f'   {key}{ds}{val!r}\n'
                )

        if indent:
            fmtstr = textwrap.indent(
                fmtstr,
                prefix=indent,
            )

        return (
            '<Context(\n'
            +
            fmtstr
            +
            f'{indent})>\n'
        )

    # NOTE: making this return a value that can be passed to
    # `eval()` is entirely **optional** dawggg B)
    # https://docs.python.org/3/library/functions.html#repr
    # https://docs.python.org/3/reference/datamodel.html#object.__repr__
    #
    # XXX: Currently we target **readability** from a (console)
    # logging perspective over `eval()`-ability since we do NOT
    # target serializing non-struct instances!
    # def __repr__(self) -> str:
    __str__ = pformat
    __repr__ = pformat

    @property
    def cancel_called(self) -> bool:
        '''
        Records whether cancellation has been requested for this context
        by a call to  `.cancel()` either due to,
        - either an explicit call by some local task,
        - or an implicit call due to an error caught inside
          the ``Portal.open_context()`` block.

        '''
        return self._cancel_called

    @cancel_called.setter
    def cancel_called(self, val: bool) -> None:
        '''
        Set the self-cancelled request `bool` value.

        '''
        # to debug who frickin sets it..
        # if val:
        #     from .devx import pause_from_sync
        #     pause_from_sync()

        self._cancel_called = val

    @property
    def canceller(self) -> tuple[str, str]|None:
        '''
        `Actor.uid: tuple[str, str]` of the (remote)
        actor-process who's task was cancelled thus causing this
        (side of the) context to also be cancelled.

        '''
        if canc := self._canceller:
            return tuple(canc)

        return None

    def _is_self_cancelled(
        self,
        remote_error: Exception|None = None,

    ) -> bool:

        if not self._cancel_called:
            return False

        re: BaseException|None = (
            remote_error
            or
            self._remote_error
        )
        # XXX we only report "this context" as self-cancelled
        # once we've received a ctxc from our direct-peer task
        # (aka we're `.cancel_acked`).
        if not re:
            return False

        if from_uid := re.src_uid:
            from_uid: tuple = tuple(from_uid)

        our_uid: tuple = self._actor.uid
        our_canceller = self.canceller

        return bool(
            isinstance((ctxc := re), ContextCancelled)
            and from_uid == self.chan.uid
            and ctxc.canceller == our_uid
            and our_canceller == our_uid
        )

    @property
    def cancel_acked(self) -> bool:
        '''
        Records whether the task on the remote side of this IPC
        context acknowledged a cancel request via a relayed
        `ContextCancelled` with the `.canceller` attr set to the
        `Actor.uid` of the local actor who's task entered
        `Portal.open_context()`.

        This will only be `True` when `.cancel()` is called and
        the ctxc response contains a `.canceller: tuple` field
        equal to the uid of the calling task's actor.

        '''
        return self._is_self_cancelled()

    @property
    def cancelled_caught(self) -> bool:
        '''
        Exactly the value of `self._scope.cancelled_caught`
        (delegation) and should only be (able to be read as)
        `True` for a `.side == "caller"` ctx wherein the
        `Portal.open_context()` block was exited due to a call to
        `._scope.cancel()` - which should only ocurr in 2 cases:

        - a caller side calls `.cancel()`, the far side cancels
          and delivers back a `ContextCancelled` (making
          `.cancel_acked == True`) and `._scope.cancel()` is
          called by `._maybe_cancel_and_set_remote_error()` which
          in turn cancels all `.open_context()` started tasks
          (including any overrun queuing ones).
          => `._scope.cancelled_caught == True` by normal `trio`
          cs semantics.

        - a caller side is delivered a `._remote_error:
          RemoteActorError` via `._deliver_msg()` and a transitive
          call to `_maybe_cancel_and_set_remote_error()` calls
          `._scope.cancel()` and that cancellation eventually
          results in `trio.Cancelled`(s) caught in the
          `.open_context()` handling around the @acm's `yield`.

        Only as an FYI, in the "callee" side case it can also be
        set but never is readable by any task outside the RPC
        machinery in `._invoke()` since,:
        - when a callee side calls `.cancel()`, `._scope.cancel()`
          is called immediately and handled specially inside
          `._invoke()` to raise a `ContextCancelled` which is then
          sent to the caller side.

          However, `._scope.cancelled_caught` can NEVER be
          accessed/read as `True` by any RPC invoked task since it
          will have terminated before the cs block exit.

        '''
        return bool(
            # the local scope was cancelled either by
            # remote error or self-request
            (self._scope and self._scope.cancelled_caught)

            # the local scope was never cancelled
            # and instead likely we received a remote side
            # # cancellation that was raised inside `.wait_for_result()`
            # or (
            #     (se := self._local_error)
            #     and se is re
            # )
        )

    # @property
    # def is_waiting_result(self) -> bool:
    #     return bool(self._res_scope)

    @property
    def side(self) -> str:
        '''
        Return string indicating which task this instance is wrapping.

        '''
        return 'parent' if self._portal else 'child'

    @staticmethod
    def _peer_side(side: str) -> str:
        match side:
            case 'child':
                return 'parent'
            case 'parent':
                return 'child'

    @property
    def peer_side(self) -> str:
        return self._peer_side(self.side)

    # TODO: remove stat!
    # -[ ] re-implement the `.experiemental._pubsub` stuff
    #     with `MsgStream` and that should be last usage?
    # -[ ] remove from `tests/legacy_one_way_streaming.py`!
    async def send_yield(
        self,
        data: Any,
    ) -> None:
        '''
        Deprecated method for what now is implemented in `MsgStream`.

        We need to rework / remove some stuff tho, see above.

        '''
        warnings.warn(
            "`Context.send_yield()` is now deprecated. "
            "Use ``MessageStream.send()``. ",
            DeprecationWarning,
            stacklevel=2,
        )
        await self.chan.send(
            Yield(
                cid=self.cid,
                pld=data,
            )
        )

    async def send_stop(self) -> None:
        '''
        Terminate a `MsgStream` dialog-phase by sending the IPC
        equiv of a `StopIteration`.

        '''
        await self.chan.send(Stop(cid=self.cid))

    def _maybe_cancel_and_set_remote_error(
        self,
        error: BaseException,

        # TODO: manual toggle for cases where we wouldn't normally
        # mark ourselves cancelled but want to?
        # set_cancel_called: bool = False,

    ) -> None:
        '''
        (Maybe) cancel this local scope due to a received remote
        error (normally via an IPC msg) which the actor runtime
        routes to this context.

        Acts as a form of "relay" for a remote error raised in the
        corresponding remote task's `Context` wherein the next time
        the local task exectutes a checkpoint, a `trio.Cancelled`
        will be raised and depending on the type and source of the
        original remote error, and whether or not the local task
        called `.cancel()` itself prior, an equivalent
        `ContextCancelled` or `RemoteActorError` wrapping the
        remote error may be raised here by any of,

        - `Portal.open_context()`
        - `Portal.result()`
        - `Context.open_stream()`
        - `Context.wait_for_result()`

        when called/closed by actor local task(s).

        NOTEs: 
          - It is expected that the caller has previously unwrapped
            the remote error using a call to `unpack_error()` and
            provides that output exception value as the input
            `error` argument *here*.

        TODOs:
          - If this is an error message from a context opened by
            `Portal.open_context()` (ideally) we want to interrupt
            any ongoing local tasks operating within that
            `Context`'s cancel-scope so as to be notified ASAP of
            the remote error and engage any caller handling (eg.
            for cross-process task supervision).

          - In some cases we may want to raise the remote error
            immediately since there is no guarantee the locally
            operating task(s) will attempt to execute a checkpoint
            any time soon; in such cases there are 2 possible
            approaches depending on the current task's work and
            wrapping "thread" type:

            - Currently we only support
              a `trio`-native-and-graceful approach: we only ever
              wait for local tasks to exec a next
              `trio.lowlevel.checkpoint()` assuming that any such
              task must do so to interact with the actor runtime
              and IPC interfaces and will then be cancelled by
              the internal `._scope` block.

            - (NOT IMPLEMENTED) system-level-aggressive: maybe we
              could eventually interrupt sync code (invoked using
              `trio.to_thread` or some other adapter layer) with
              a signal (a custom unix one for example?
              https://stackoverflow.com/a/5744185) depending on the
              task's wrapping thread-type such that long running
              sync code should never cause the delay of actor
              supervision tasks such as cancellation and respawn
              logic.

        '''
        # XXX: currently this should only be used when
        # `Portal.open_context()` has been opened since it's
        # assumed that other portal APIs like,
        #  - `Portal.run()`,
        #  - `ActorNursery.run_in_actor()`
        # do their own error checking at their own call points and
        # result processing.

        # TODO: never do this right?
        # if self._remote_error:
        #     return

        # XXX: denote and set the remote side's error so that
        # after we cancel whatever task is the opener of this
        # context, it can raise or swallow that error
        # appropriately.
        log.runtime(
            'Setting remote error for ctx\n\n'
            f'<= {self.peer_side!r}: {self.chan.uid}\n'
            f'=> {self.side!r}: {self._actor.uid}\n\n'
            f'{error!r}'
        )
        self._remote_error: BaseException = error

        msgerr: bool = False

        # self-cancel (ack) or,
        # peer propagated remote cancellation.
        if isinstance(error, ContextCancelled):
            # NOTE in the case error is a ctxc the canceller will
            # either be another peer or us. in the case where it's us
            # we mark ourself as the canceller of ourselves (a ctx
            # "self cancel" from this side's perspective), if instead
            # the far end was cancelled by some other (inter-) peer,
            # we want to mark our canceller as the actor that was
            # cancelled, NOT their reported canceller. IOW in the
            # latter case we're cancelled by someone else getting
            # cancelled.
            if (canc := error.canceller) == self._actor.uid:
                whom: str = 'us'
                self._canceller = canc
            else:
                whom = 'a remote peer (not us)'
                self._canceller = error.src_uid

            log.cancel(
                f'IPC context was cancelled by {whom}!\n\n'
                f'{error}'
            )

        elif isinstance(error, MsgTypeError):
            msgerr = True
            self._canceller = error.src_uid
            log.error(
                f'IPC dialog error due to msg-type caused by {self.peer_side!r} side\n\n'
                f'{error}\n'
                f'{pformat(self)}\n'
            )

        else:
            # always record the cancelling actor's uid since its
            # cancellation state is linked and we want to know
            # which process was the cause / requester of the
            # cancellation.
            maybe_error_src_uid: tuple = getattr(
                error,
                'src_uid',
                None,
            )
            # we mark the source actor as our canceller
            self._canceller = maybe_error_src_uid
            log.error(
                f'Remote context error:\n\n'
                # f'{pformat(self)}\n'
                f'{error!r}'
            )

        if self._canceller is None:
            log.error('Ctx has no canceller set!?')

        # Cancel the local `._scope`, catch that
        # `._scope.cancelled_caught` and re-raise any remote error
        # once exiting (or manually calling `.wait_for_result()`) the
        # `.open_context()`  block.
        cs: trio.CancelScope = self._scope
        if (
            cs

            # XXX this is an expected cancel request response
            # message and we **don't need to raise it** in the
            # local cancel `._scope` since it will potentially
            # override a real error. After this method returns
            # if `._cancel_called` then `.cancel_acked and .cancel_called`
            # always should be set.
            and not self._is_self_cancelled()
            and not cs.cancel_called
            and not cs.cancelled_caught
        ):
            if (
                msgerr

                # NOTE: we allow user to config not cancelling the
                # local scope on `MsgTypeError`s
                and
                not self._cancel_on_msgerr
            ):
                message: str = (
                    'NOT Cancelling `Context._scope` since,\n'
                    f'Context._cancel_on_msgerr = {self._cancel_on_msgerr}\n\n'
                    f'AND we got a msg-type-error!\n'
                    f'{error}\n'
                )
            else:
                # TODO: it'd sure be handy to inject our own
                # `trio.Cancelled` subtype here ;)
                # https://github.com/goodboy/tractor/issues/368
                message: str = 'Cancelling `Context._scope` !\n\n'
                # from .devx import pause_from_sync
                # pause_from_sync()
                self._scope.cancel()
        else:
            message: str = 'NOT cancelling `Context._scope` !\n\n'
            # from .devx import mk_pdb
            # mk_pdb().set_trace()

        fmt_str: str = 'No `self._scope: CancelScope` was set/used ?\n'
        if (
            cs
            and
            at_least_level(log=log, level='cancel')
        ):
            fmt_str: str = self.pformat(
                extra_fields={
                    '._is_self_cancelled()': self._is_self_cancelled(),
                    '._cancel_on_msgerr': self._cancel_on_msgerr,
                }
            )
            from .devx.pformat import pformat_cs
            cs_fmt: str = pformat_cs(
                cs,
                var_name='Context._scope',
            )
            fmt_str += (
                '\n'
                +
                cs_fmt
            )
        log.cancel(
            message
            +
            fmt_str
        )

    # TODO: also add to `Channel`?
    @property
    def dst_maddr(self) -> str:
        chan: Channel = self.chan
        trans: MsgTransport = chan.transport
        # cid: str = self.cid
        # cid_head, cid_tail = cid[:6], cid[-6:]
        return trans.maddr

    dmaddr = dst_maddr

    @property
    def repr_rpc(self) -> str:
        # TODO: how to show the transport interchange fmt?
        # codec: str = self.chan.transport.codec_key
        outcome_str: str = self.repr_outcome(
            show_error_fields=True,
            type_only=True,
        )
        return (
            # f'{self._nsf}() -{{{codec}}}-> {repr(self.outcome)}:'
            f'{self._nsf}() -> {outcome_str}'
        )

    @property
    def repr_caller(self) -> str:
        ci: CallerInfo|None = self._caller_info
        if ci:
            return (
                f'{ci.caller_nsp}()'
                # f'|_api: {ci.api_nsp}'
            )

        return '<UNKNOWN caller-frame>'

    @property
    def repr_api(self) -> str:
        return 'Portal.open_context()'

        # TODO: use `.dev._frame_stack` scanning to find caller!
        # ci: CallerInfo|None = self._caller_info
        # if ci:
        #     return (
        #         f'{ci.api_nsp}()\n'
        #     )

    async def cancel(
        self,
        timeout: float = 0.616,

    ) -> None:
        '''
        Cancel this inter-actor IPC context by requestng the
        remote side's cancel-scope-linked `Task` by calling
        `._scope.cancel()` and delivering an `ContextCancelled`
        ack msg in reponse.

        Behaviour:
        ---------
        - after the far end cancels, the `.cancel()` calling side
          should receive a `ContextCancelled` with the
          `.canceller: tuple` uid set to the current `Actor.uid`.

        - timeout (quickly) on failure to rx this ACK error-msg in
          an attempt to sidestep 2-generals when the transport
          layer fails.

        Note, that calling this method DOES NOT also necessarily
        result in `Context._scope.cancel()` being called
        **locally**!

        => That is, an IPC `Context` (this) **does not**
           have the same semantics as a `trio.CancelScope`.

        If the caller (who entered the `Portal.open_context()`)
        desires that the internal block's cancel-scope  be
        cancelled it should open its own `trio.CancelScope` and
        manage it as needed.

        '''
        side: str = self.side
        # XXX for debug via the `@.setter`
        self.cancel_called = True

        header: str = (
            f'Cancelling ctx from {side.upper()}-side\n'
        )
        reminfo: str = (
            # ' =>\n'
            # f'Context.cancel() => {self.chan.uid}\n'
            f'\n'
            f'c)=> {self.chan.uid}\n'
            f'   |_[{self.dst_maddr}\n'
            f'     >>{self.repr_rpc}\n'
            # f'    >> {self._nsf}() -> {codec}[dict]:\n\n'
            # TODO: pull msg-type from spec re #320
        )

        # CALLER side task
        # ------ - ------
        # Aka the one that entered `Portal.open_context()`
        #
        # NOTE: on the call side we never manually call
        # `._scope.cancel()` since we expect the eventual
        # `ContextCancelled` from the other side to trigger this
        # when the runtime finally receives it during teardown
        # (normally in `.wait_for_result()` called from
        # `Portal.open_context().__aexit__()`)
        if side == 'parent':
            if not self._portal:
                raise InternalError(
                    'No portal found!?\n'
                    'Why is this supposed {self.side!r}-side ctx task missing it?!?'
                )

            cid: str = self.cid
            with trio.move_on_after(timeout) as cs:
                cs.shield = True
                log.cancel(
                    header
                    +
                    reminfo
                )

                # NOTE: we're telling the far end actor to cancel a task
                # corresponding to *this actor*. The far end local channel
                # instance is passed to `Actor._cancel_task()` implicitly.
                await self._portal.run_from_ns(
                    'self',
                    '_cancel_task',
                    cid=cid,
                )

            if cs.cancelled_caught:
                # XXX: there's no way to know if the remote task was indeed
                # cancelled in the case where the connection is broken or
                # some other network error occurred.
                # if not self._portal.channel.connected():
                if not self.chan.connected():
                    log.cancel(
                        'May have failed to cancel remote task?\n'
                        f'{reminfo}'
                    )
                else:
                    log.cancel(
                        f'Timed out on cancel request of remote task?\n'
                        f'\n'
                        f'{reminfo}'
                    )

        # CALLEE side task
        # ------ - ------
        # Aka the one that DID NOT EVER enter a `Portal.open_context()`
        # and instead was constructed and scheduled as an
        # `_invoke()` RPC task.
        #
        # NOTE: on this side we ALWAYS cancel the local scope
        # since the caller expects a `ContextCancelled` to be sent
        # from `._runtime._invoke()` back to the other side. The
        # logic for catching the result of the below
        # `._scope.cancel()` is inside the `._runtime._invoke()`
        # context RPC handling block.
        else:
            log.cancel(
                header
                +
                reminfo
            )
            # TODO: should we have an explicit cancel message
            # or is relaying the local `trio.Cancelled` as an
            # {'error': trio.Cancelled, cid: "blah"} enough?
            # This probably gets into the discussion in
            # https://github.com/goodboy/tractor/issues/36
            assert self._scope
            self._scope.cancel()

    # TODO: replace all the `._maybe_raise_remote_err()` usage
    # with instances of this!!
    def maybe_raise(
        self,
        hide_tb: bool = True,
        **kwargs,

    ) -> Exception|None:
        '''
        Check for for a remote error delivered by the runtime from
        our peer (task); if set immediately raise.

        This is a convenience wrapper for
        `._maybe_raise_remote_err(self._remote_error)`.

        '''
        __tracebackhide__: bool = hide_tb
        if re := self._remote_error:
            return self._maybe_raise_remote_err(
                re,
                hide_tb=hide_tb,
                **kwargs,
            )

    def _maybe_raise_remote_err(
        self,
        remote_error: Exception,

        from_src_exc: BaseException|None|bool = False,
        raise_ctxc_from_self_call: bool = False,
        raise_overrun_from_self: bool = True,
        hide_tb: bool = True,

    ) -> (
        ContextCancelled  # `.cancel()` request to far side
        |RemoteActorError  # stream overrun caused and ignored by us
    ):
        '''
        Maybe raise a remote error depending on the type of error and
        *who*, i.e. which side of the task pair across actors,
        requested a cancellation (if any).

        Depending on the input config-params suppress raising
        certain remote excs:

        - if `remote_error: ContextCancelled` (ctxc) AND this side's
          task is the "requester", it at somem point called
          `Context.cancel()`, then the peer's ctxc is treated
          as a "cancel ack".

         |_ this behaves exactly like how `trio.Nursery.cancel_scope`
            absorbs any `BaseExceptionGroup[trio.Cancelled]` wherein the
            owning parent task never will raise a `trio.Cancelled`
            if `CancelScope.cancel_called == True`.

        - `remote_error: StreamOverrrun` (overrun) AND
           `raise_overrun_from_self` is set.

        '''
        __tracebackhide__: bool = hide_tb
        peer_uid: tuple = self.chan.uid

        # XXX NOTE XXX: `ContextCancelled`/`StreamOverrun` absorption
        # for "graceful cancellation" case(s):
        #
        # Whenever a "side" of a context (a `Task` running in
        # an actor) **is** the side which requested ctx
        # cancellation (likekly via ``Context.cancel()``), we
        # **don't** want to re-raise any eventually received
        # `ContextCancelled` response locally (would be akin to
        # a `trio.Nursery` nursery raising `trio.Cancelled`
        # whenever `CancelScope.cancel()` was called).
        #
        # Instead, silently reap the remote delivered ctxc
        # (`ContextCancelled`) as an expected
        # error-msg-is-cancellation-ack IFF said
        # `remote_error: ContextCancelled` has `.canceller`
        # set to the `Actor.uid` of THIS task (i.e. the
        # cancellation requesting task's actor is the actor
        # checking whether it should absorb the ctxc).
        self_ctxc: bool = self._is_self_cancelled(remote_error)
        if (
            self_ctxc
            and
            not raise_ctxc_from_self_call

            # TODO: ?potentially it is useful to emit certain
            # warning/cancel logs for the cases where the
            # cancellation is due to a lower level cancel
            # request, such as `Portal.cancel_actor()`, since in
            # that case it's not actually this specific ctx that
            # made a `.cancel()` call, but it is the same
            # actor-process?
            # or self.chan._cancel_called
            # XXX: ^ should we have a special separate case
            # for this ^, NO right?

        ) or (
            # NOTE: whenever this side is the cause of an
            # overrun on the peer side, i.e. we sent msgs too
            # fast and the peer task was overrun according
            # to `MsgStream` buffer settings, AND this was
            # called with `raise_overrun_from_self=True` (the
            # default), silently absorb any `StreamOverrun`.
            #
            # XXX, this is namely useful for supressing such faults
            # during cancellation/error/final-result handling inside
            # `.msg._ops.drain_to_final_msg()` such that we do not
            # raise during a cancellation-request, i.e. when
            # `._cancel_called == True`.
            #
            not raise_overrun_from_self
            and isinstance(remote_error, RemoteActorError)
            and remote_error.boxed_type is StreamOverrun

            # and tuple(remote_error.msgdata['sender']) == peer_uid
            and tuple(remote_error.sender) == peer_uid
        ):
            # NOTE: we set the local scope error to any "self
            # cancellation" error-response thus "absorbing"
            # the error silently B)
            if self._local_error is None:
                self._local_error = remote_error

            else:
                log.warning(
                    'Local error already set for ctx?\n'
                    f'{self._local_error}\n\n'
                    f'{self}'
                )

            return remote_error

        # NOTE: currently we are hiding underlying runtime errors
        # which are often superfluous to user handler code. not
        # sure if this is still needed / desired for all operation?
        # TODO: maybe we can only NOT mask if:
        # - [ ] debug mode is enabled or,
        # - [ ] a certain log level is set?
        # - [ ] consider using `.with_traceback()` to filter out
        #       runtime frames from the tb explicitly?
        # https://docs.python.org/3/reference/simple_stmts.html#the-raise-statement
        # https://stackoverflow.com/a/24752607
        if from_src_exc is not False:
            raise remote_error from from_src_exc

        raise remote_error

    async def wait_for_result(
        self,
        hide_tb: bool = True,

    ) -> Any|Exception:
        '''
        From some (caller) side task, wait for and return the final
        result from the remote (callee) side's task.

        This provides a mechanism for one task running in some actor to wait
        on another task at the other side, in some other actor, to terminate.

        If the remote task is still in a streaming state (it is delivering
        values from inside a ``Context.open_stream():`` block, then those
        msgs are drained but discarded since it is presumed this side of
        the context has already finished with its own streaming logic.

        If the remote context (or its containing actor runtime) was
        canceled, either by a local task calling one of
        ``Context.cancel()`` or `Portal.cancel_actor()``, we ignore the
        received ``ContextCancelled`` exception if the context or
        underlying IPC channel is marked as having been "cancel called".
        This is similar behavior to using ``trio.Nursery.cancel()``
        wherein tasks which raise ``trio.Cancel`` are silently reaped;
        the main different in this API is in the "cancel called" case,
        instead of just not raising, we also return the exception *as
        the result* since client code may be interested in the details
        of the remote cancellation.

        '''
        __tracebackhide__: bool = hide_tb
        if not self._portal:
            raise RuntimeError(
                'Invalid usage of `Context.wait_for_result()`!\n'
                'Not valid on child-side IPC ctx!\n'
            )
        if self._final_result_is_set():
            return self._result

        assert self._rx_chan
        raise_overrun: bool = not self._allow_overruns
        if (
            self.maybe_error is None
            and
            not self._rx_chan._closed  # type: ignore
        ):
            # wait for a final context result/error by "draining"
            # (by more or less ignoring) any bi-dir-stream "yield"
            # msgs still in transit from the far end.
            #
            # XXX NOTE XXX: this call shouldn't really ever raise
            # (other then internal error), instead delivering an
            # `Error`-msg  and that being `.maybe_raise()`-ed below
            # since every message should be delivered via the normal
            # `._deliver_msg()` route which will appropriately set
            # any `.maybe_error`.
            outcome_msg: Return|Error|ContextCancelled
            drained_msgs: list[MsgType]
            (
                outcome_msg,
                drained_msgs,
            ) = await msgops.drain_to_final_msg(
                ctx=self,
                hide_tb=hide_tb,
            )
            drained_status: str = (
                'Ctx drained to final outcome msg\n\n'
                f'{outcome_msg}\n'
            )

            # ?XXX, should already be set in `._deliver_msg()` right?
            if self._outcome_msg is not Unresolved:
                # from .devx import _debug
                # await _debug.pause()
                assert self._outcome_msg is outcome_msg
            else:
                self._outcome_msg = outcome_msg

            if drained_msgs:
                drained_status += (
                    '\n'
                    f'The pre-drained msgs are\n'
                    f'{pformat(drained_msgs)}\n'
                )

            log.cancel(drained_status)

        self.maybe_raise(
            # NOTE: obvi we don't care if we
            # overran the far end if we're already
            # waiting on a final result (msg).
            raise_overrun_from_self=(
                raise_overrun
                and
                # only when we ARE NOT the canceller
                # should we raise overruns, bc ow we're
                # raising something we know might happen
                # during cancellation ;)
                (not self._cancel_called)
            ),
            hide_tb=hide_tb,
        )
        # TODO: eventually make `.outcome: Outcome` and thus return
        # `self.outcome.unwrap()` here?
        return self.outcome

    # TODO: switch this with above!
    # -[ ] should be named `.wait_for_outcome()` and instead do
    #     a `.outcome.Outcome.unwrap()` ?
    #
    async def result(
        self,
        *args,
        **kwargs,
    ) -> Any|Exception:
        log.warning(
            '`Context.result()` is DEPRECATED!\n'
            'Use `Context.[no]wait_for_result()` instead!\n'
        )
        return await self.wait_for_result(
            *args,
            **kwargs,
        )

    @property
    def maybe_error(self) -> BaseException|None:
        '''
        Return the (remote) error as outcome or `None`.

        Remote errors take precedence over local ones.

        '''
        le: BaseException|None = self._local_error
        re: RemoteActorError|ContextCancelled|None = self._remote_error

        match (le, re):
            # NOTE: remote errors always get precedence since even
            # in the cases where a local error was the cause, the
            # received boxed ctxc should include the src info
            # caused by us right?
            case (
                _,
                RemoteActorError(),
            ):
                # give precedence to remote error if it's
                # NOT a cancel ack (ctxc).
                return (
                    re or le
                )

        # TODO: extra logic to handle ctxc ack case(s)?
        # -[ ] eg. we error, call .cancel(), rx ack but should
        #      raise the _local_error instead?
        # -[ ] are there special error conditions where local vs.
        #      remote should take precedence?
            # case (
            #     _,
            #     ContextCancelled(canceller=),
            # ):

        error: BaseException|None = le or re
        if error:
            return error

        if cancmsg := self._cancel_msg:
            # NOTE: means we're prolly in the process of
            # processing the cancellation caused by
            # this msg (eg. logging from `Actor._cancel_task()`
            # method after receiving a `Context.cancel()` RPC)
            # though there shouldn't ever be a `._cancel_msg`
            # without it eventually resulting in this property
            # delivering a value!
            log.debug(
                '`Context._cancel_msg` is set but has not yet resolved to `.maybe_error`?\n\n'
                f'{cancmsg}\n'
            )

        # assert not self._cancel_msg
        return None

    def _final_result_is_set(self) -> bool:
        return self._result is not Unresolved

    # def get_result_nowait(self) -> Any|None:
    # def get_outcome_nowait(self) -> Any|None:
    # def recv_result_nowait(self) -> Any|None:
    # def receive_outcome_nowait(self) -> Any|None:
    # TODO: use `outcome.Outcome` here instead?
    @property
    def outcome(self) -> (
        Any|
        RemoteActorError|
        ContextCancelled
        # TODO: make this a `outcome.Outcome`!
    ):
        '''
        Return the "final outcome" (state) of the far end peer task
        non-blocking. If the remote task has not completed then this
        field always resolves to the module defined `Unresolved`
        handle.

        ------ - ------
        TODO->( this is doc-driven-dev content not yet actual ;P )

        The final "outcome" from an IPC context which can be any of:
        - some `outcome.Value` which boxes the returned output from the peer task's 
          `@context`-decorated remote task-as-func, or
        - an `outcome.Error` wrapping an exception raised that same RPC task
          after a fault or cancellation, or
        - an unresolved `outcome.Outcome` when the peer task is still
          executing and has not yet completed.

        '''
        return (
            self.maybe_error
            or
            self._result
        )

    @property
    def has_outcome(self) -> bool:
        return bool(self.maybe_error) or self._final_result_is_set()

    def repr_outcome(
        self,
        show_error_fields: bool = False,
        type_only: bool = False,

    ) -> str:
        '''
        Deliver a (simplified) `str` representation (as in
        `.__repr__()`) of the final `.outcome`

        '''
        merr: Exception|None = self.maybe_error
        if merr:
            if type_only:
                return type(merr).__name__

            # if the error-type is one of ours and has the custom
            # defined "repr-(in)-one-line" method call it, ow
            # just deliver the type name.
            if (
                (reprol := getattr(merr, 'reprol', False))
                and
                show_error_fields
            ):
                return reprol()

            elif isinstance(merr, BaseExceptionGroup):
                # TODO: maybe for multis we should just show
                # a one-line count per error type, like with
                # `collections.Counter`?
                #
                # just the type name for now to avoid long lines
                # when tons of cancels..
                return (
                    str(type(merr).__name__)
                    or
                    repr(merr)
                )

            # for all other errors show their regular output
            return (
                str(merr)
                or
                repr(merr)
            )

        return (
            str(self._result)
            or
            repr(self._result)
        )

    @property
    def repr_state(self) -> str:
        '''
        A `str`-status describing the current state of this
        inter-actor IPC context in terms of the current "phase" state
        of the SC shuttling dialog protocol.

        '''
        merr: Exception|None = self.maybe_error
        outcome: Unresolved|Exception|Any = self.outcome
        status: str|None = None
        match (
            outcome,
            merr,
        ):
            # "graceful" ctx cancellation
            case (
                Unresolved,
                ContextCancelled(),
            ):
                if self._is_self_cancelled():
                    status = 'self-cancelled'
                elif (
                    self.canceller
                    and not self._cancel_called
                ):
                    status = 'peer-cancelled'

            # (remote) error condition
            case (
                Unresolved,
                BaseException(),  # any error-type
            ):
                status = 'errored'

            # result already returned
            case (
                _,  # any non-unresolved value
                None,
            ) if self._final_result_is_set():
                status = 'result-returned'

            # normal operation but still in a pre-`Return`-result
            # dialog phase
            case (
                Unresolved,  # noqa (ruff, you so weird..)
                None,  # no (remote) error set
            ):
                if stream := self._stream:
                    if stream.closed:
                        status = 'streaming-finished'
                    else:
                        status = 'streaming'

                elif self._started_called:
                    status = 'started'

                else:
                    if self.side == 'child':
                        status = 'pre-started'
                    else:
                        status = 'syncing-to-child'

        if status is None:
            status = '??unknown??'
            # from tractor.devx import mk_pdb
            # mk_pdb().set_trace()

        return status

    async def started(
        self,

        value: PayloadT|None = None,
        validate_pld_spec: bool = True,
        strict_pld_parity: bool = False,

        # TODO: this will always emit for msgpack for any () vs. []
        # inside the value.. do we want to offer warnings on that?
        # complain_no_parity: bool = False,

        hide_tb: bool = True,

    ) -> None:
        '''
        Indicate to calling actor's task that this linked context
        has started and send ``value`` to the other side via IPC.

        On the calling side ``value`` is the second item delivered
        in the tuple returned by ``Portal.open_context()``.

        '''
        if self._portal:
            raise RuntimeError(
                f'Caller side context {self} can not call started!'
            )

        elif self._started_called:
            raise RuntimeError(
                f'called `.started()` twice on context with {self.chan.uid}'
            )

        started_msg = Started(
            cid=self.cid,
            pld=value,
        )
        # XXX MEGA NOTE XXX: ONLY on the first msg sent with
        # `Context.started()` do we STRINGENTLY roundtrip-check
        # the first payload such that the child side can't send an
        # incorrect value according to the currently applied
        # msg-spec!
        #
        # HOWEVER, once a stream is opened via
        # `Context.open_stream()` then this check is NEVER done on
        # `MsgStream.send()` and instead both the parent and child
        # sides are expected to relay back msg-type errors when
        # decode failures exhibit on `MsgStream.receive()` calls thus
        # enabling a so-called (by the holy 0mq lords)
        # "cheap-or-nasty pattern" un-protocol design Bo
        #
        # https://zguide.zeromq.org/docs/chapter7/#The-Cheap-or-Nasty-Pattern
        #
        __tracebackhide__: bool = hide_tb
        if validate_pld_spec:
            # TODO: prolly wrap this as a `show_frame_when_not()`
            try:
                msgops.validate_payload_msg(
                    pld_msg=started_msg,
                    pld_value=value,
                    ipc=self,
                    strict_pld_parity=strict_pld_parity,
                    hide_tb=hide_tb,
                )
            except BaseException as _bexc:
                err = _bexc
                if not isinstance(err, MsgTypeError):
                    __tracebackhide__: bool = False

                raise err

        # TODO: maybe a flag to by-pass encode op if already done
        # here in caller?
        await self.chan.send(started_msg)

        # set msg-related internal runtime-state
        self._started_called: bool = True
        self._started_msg: Started = started_msg
        self._started_pld = value

    async def _drain_overflows(
        self,
    ) -> None:
        '''
        Private task spawned to push newly received msgs to the local
        task which getting overrun by the remote side.

        In order to not block the rpc msg loop, but also not discard
        msgs received in this context, we need to async push msgs in
        a new task which only runs for as long as the local task is in
        an overrun state.

        '''
        self._in_overrun = True
        try:
            while self._overflow_q:
                # NOTE: these msgs should never be errors since we always do
                # the check prior to checking if we're in an overrun state
                # inside ``._deliver_msg()``.
                msg = self._overflow_q.popleft()
                try:
                    await self._send_chan.send(msg)
                except trio.BrokenResourceError:
                    log.warning(
                        f"{self._send_chan} consumer is already closed"
                    )
                    return
                except trio.Cancelled:
                    # we are obviously still in overrun
                    # but the context is being closed anyway
                    # so we just warn that there are un received
                    # msgs still..
                    self._overflow_q.appendleft(msg)
                    fmt_msgs = ''
                    for msg in self._overflow_q:
                        fmt_msgs += f'{pformat(msg)}\n'

                    log.warning(
                        f'Context for {self.cid} is being closed while '
                        'in an overrun state!\n'
                        'Discarding the following msgs:\n'
                        f'{fmt_msgs}\n'
                    )
                    raise

        finally:
            # task is now finished with the backlog so mark us as
            # no longer in backlog.
            self._in_overrun = False

    async def _deliver_msg(
        self,
        msg: MsgType,

    ) -> bool:
        '''
        Deliver an IPC msg received from a transport-channel to
        this context's underlying mem chan for handling by local
        user application tasks; deliver `bool` indicating whether
        the msg was able to be delivered.

        If `._allow_overruns == True` (maybe) append the msg to an
        "overflow queue" and start a "drainer task" (inside the
        `._scope_nursery: trio.Nursery`) which ensures that such
        messages are queued up and eventually sent if possible.

         XXX RULES XXX
        ------ - ------
        - NEVER raise remote errors from this method; a runtime task caller.
          An error "delivered" to a ctx should always be raised by
          the corresponding local task operating on the
          `Portal`/`Context` APIs.

        - NEVER `return` early before delivering the msg!
          bc if the error is a ctxc and there is a task waiting on
          `.wait_for_result()` we need the msg to be
          `send_chan.send_nowait()`-ed over the `._rx_chan` so
          that the error is relayed to that waiter task and thus
          raised in user code!

        '''
        cid: str = self.cid
        chan: Channel = self.chan
        from_uid: tuple[str, str]  = chan.uid
        send_chan: trio.MemorySendChannel = self._send_chan
        nsf: NamespacePath = self._nsf

        side: str = self.side
        if side == 'child':
            assert not self._portal

        flow_body: str = (
            f'<= peer {self.peer_side!r}: {from_uid}\n'
            f'  |_<{nsf}()>\n\n'

            f'=> {side!r}: {self._task}\n'
            f'  |_<{self.repr_api} @ {self.repr_caller}>\n\n'
        )

        re: Exception|None
        if re := unpack_error(
            msg,
            self.chan,
        ):
            if not isinstance(re, ContextCancelled):
                log_meth = log.error
            else:
                log_meth = log.runtime

            log_meth(
                f'Delivering IPC ctx error from {self.peer_side!r} to {side!r} task\n\n'

                f'{flow_body}'
                f'{pformat(re)}\n'
            )
            self._cancel_msg: dict = msg

            # XXX NOTE: this will not raise an error, merely set
            # `._remote_error` and maybe cancel any task currently
            # entered in `Portal.open_context()` presuming the
            # error is "cancel causing" (i.e. a `ContextCancelled`
            # or `RemoteActorError`).
            self._maybe_cancel_and_set_remote_error(re)

        # TODO: expose as mod func instead!
        structfmt = pretty_struct.Struct.pformat
        if self._in_overrun:
            report: str = (
                f'{flow_body}'
                f'{structfmt(msg)}\n'
            )
            over_q: deque = self._overflow_q
            self._overflow_q.append(msg)

            if len(over_q) == over_q.maxlen:
                report = (
                    'FAILED to queue OVERRUN msg, OVERAN the OVERRUN QUEUE !!\n\n'
                    + report
                )
                # log.error(report)
                log.debug(report)

            else:
                report = (
                    'Queueing OVERRUN msg on caller task:\n\n'
                    + report
                )
                log.debug(report)

            # XXX NOTE XXX
            # overrun is the ONLY case where returning early is fine!
            return False

        try:
            log.runtime(
                f'Delivering msg from IPC ctx:\n\n'

                f'{flow_body}'

                f'{structfmt(msg)}\n'
            )
            # NOTE: if an error is deteced we should always still
            # send it through the feeder-mem-chan and expect
            # it to be raised by any context (stream) consumer
            # task via the consumer APIs on both the `Context` and
            # `MsgStream`!
            #
            # XXX the reason is that this method is always called
            # by the IPC msg handling runtime task and that is not
            # normally the task that should get cancelled/error
            # from some remote fault!
            send_chan.send_nowait(msg)
            match msg:
                case Stop():
                    if (stream := self._stream):
                        stream._stop_msg = msg

                case Return():
                    if not self._outcome_msg:
                        log.warning(
                            f'Setting final outcome msg AFTER '
                            f'`._rx_chan.send()`??\n'
                            f'\n'
                            f'{msg}'
                        )
                        self._outcome_msg = msg

            return True

        except trio.BrokenResourceError:
            # TODO: what is the right way to handle the case where the
            # local task has already sent a 'stop' / StopAsyncInteration
            # to the other side but and possibly has closed the local
            # feeder mem chan? Do we wait for some kind of ack or just
            # let this fail silently and bubble up (currently)?

            # XXX: local consumer has closed their side
            # so cancel the far end streaming task
            log.warning(
                'Rx chan for `Context` alfready closed?\n'
                f'cid: {self.cid}\n'
                'Failed to deliver msg:\n'
                f'send_chan: {send_chan}\n\n'

                f'{pformat(msg)}\n'
            )
            return False

        # NOTE XXX: by default we do **not** maintain context-stream
        # backpressure and instead opt to relay stream overrun errors to
        # the sender; the main motivation is that using bp can block the
        # msg handling loop which calls into this method!
        except trio.WouldBlock:

            # XXX: always push an error even if the local receiver
            # is in overrun state - i.e. if an 'error' msg is
            # delivered then
            # `._maybe_cancel_and_set_remote_error(msg)` should
            # have already been called above!
            #
            # XXX QUESTION XXX: if we rx an error while in an
            # overrun state and that msg isn't stuck in an
            # overflow queue what happens?!?

            local_uid = self._actor.uid
            txt: str = (
                'on IPC context:\n'

                f'<= sender: {from_uid}\n'
                f'  |_ {self._nsf}()\n\n'

                f'=> overrun: {local_uid}\n'
                f'  |_cid: {cid}\n'
                f'  |_task: {self._task}\n'
            )
            if not self._stream_opened:
                txt += (
                    f'\n*** No stream open on `{local_uid[0]}` side! ***\n\n'
                    f'{msg}\n'
                )

            # XXX: lul, this really can't be backpressure since any
            # blocking here will block the entire msg loop rpc sched for
            # a whole channel.. maybe we should rename it?
            if self._allow_overruns:
                txt += (
                    '\n*** Starting overflow queuing task on msg ***\n\n'
                    f'{msg}\n'
                )
                log.warning(txt)
                if (
                    not self._in_overrun
                ):
                    self._overflow_q.append(msg)
                    tn: trio.Nursery = self._scope_nursery
                    assert not tn.child_tasks
                    try:
                        tn.start_soon(
                            self._drain_overflows,
                        )
                        return True

                    except RuntimeError:
                        # if the nursery is already cancelled due to
                        # this context exiting or in error, we ignore
                        # the nursery error since we never expected
                        # anything different.
                        return False
            else:
                # raise local overrun and immediately pack as IPC
                # msg for far end.
                err_msg: Error = pack_from_raise(
                    local_err=StreamOverrun(
                        txt,
                        sender=from_uid,
                    ),
                    cid=cid,
                )
                try:
                    # relay condition to sender side remote task
                    await chan.send(err_msg)
                    return True

                # XXX: local consumer has closed their side of
                # the IPC so cancel the far end streaming task
                except trio.BrokenResourceError:
                    log.warning(
                        'Channel for ctx is already closed?\n'
                        f'|_{chan}\n'
                    )

            # ow, indicate unable to deliver by default
            return False

    # NOTE: similar to `Portal.open_context()`, this impl is found in
    # the `._streaming`` mod to make reading/groking the details
    # simpler code-org-wise.
    open_stream = open_stream_from_ctx


# TODO: exception tb masking by using a manual
# `.__aexit__()`/.__aenter__()` pair on a type?
# => currently this is one of the few places we can't easily
# mask errors - on the exit side of a `Portal.open_context()`..
# there's # => currently this is one of the few places we can't
# there's 2 ways to approach it:
# - manually write an @acm type as per above
# - use `contextlib.AsyncContextDecorator` to override the default
#   impl to suppress traceback frames:
#  * https://docs.python.org/3/library/contextlib.html#contextlib.AsyncContextDecorator
#  * https://docs.python.org/3/library/contextlib.html#contextlib.ContextDecorator
# - also we could just override directly the underlying
#   `contextlib._AsyncGeneratorContextManager`?
@acm
async def open_context_from_portal(
    portal: Portal,
    func: Callable,

    allow_overruns: bool = False,
    hide_tb: bool = True,

    # proxied to RPC
    **kwargs,

) -> AsyncGenerator[tuple[Context, Any], None]:
    '''
    Open an inter-actor "task context"; a remote task is
    scheduled and cancel-scope-state-linked to a `trio.run()` across
    memory boundaries in another actor's runtime.

    This is an `@acm` API bound as `Portal.open_context()` which
    allows for deterministic setup and teardown of a remotely
    scheduled task in another remote actor. Once opened, the 2 now
    "linked" tasks run completely in parallel in each actor's
    runtime with their enclosing `trio.CancelScope`s kept in
    a synced state wherein if either side errors or cancels an
    equivalent error is relayed to the other side via an SC-compat
    IPC protocol.

    The yielded `tuple` is a pair delivering a `tractor.Context`
    and any first value "sent" by the "callee" task via a call
    to `Context.started(<value: Any>)`; this side of the
    context does not unblock until the "callee" task calls
    `.started()` in similar style to `trio.Nursery.start()`.
    When the "callee" (side that is "called"/started by a call
    to *this* method) returns, the caller side (this) unblocks
    and any final value delivered from the other end can be
    retrieved using the `Contex.wait_for_result()` api.

    The yielded ``Context`` instance further allows for opening
    bidirectional streams, explicit cancellation and
    structurred-concurrency-synchronized final result-msg
    collection. See ``tractor.Context`` for more details.

    '''
    __tracebackhide__: bool = hide_tb

    # denote this frame as a "runtime frame" for stack
    # introspection where we report the caller code in logging
    # and error message content.
    # NOTE: 2 bc of the wrapping `@acm`
    __runtimeframe__: int = 2  # noqa

    # if NOT an async func but decorated with `@context`, error.
    if (
        not inspect.iscoroutinefunction(func)
        and getattr(func, '_tractor_context_meta', False)
    ):
        raise TypeError(
            f'{func!r} must be an async function!'
        )

    ctx_meta: dict[str, Any]|None = getattr(
        func,
        '_tractor_context_meta',
        None,
    )

    # TODO: i think from here onward should probably
    # just be factored into an `@acm` inside a new
    # a new `_context.py` mod.
    nsf = NamespacePath.from_ref(func)

    # XXX NOTE XXX: currenly we do NOT allow opening a contex
    # with "self" since the local feeder mem-chan processing
    # is not built for it.
    if (uid := portal.channel.uid) == portal.actor.uid:
        raise RuntimeError(
            '** !! Invalid Operation !! **\n'
            'Can not open an IPC ctx with the local actor!\n'
            f'|_{portal.actor}\n'
        )

    ctx: Context = await portal.actor.start_remote_task(
        portal.channel,
        nsf=nsf,
        kwargs=kwargs,

        portal=portal,

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
    assert ctx._caller_info
    prior_ctx_tok: Token = _ctxvar_Context.set(ctx)

    # placeholder for any exception raised in the runtime
    # or by user tasks which cause this context's closure.
    scope_err: BaseException|None = None
    ctxc_from_callee: ContextCancelled|None = None
    try:
        async with (
            trio.open_nursery(
                strict_exception_groups=False,
            ) as tn,

            msgops.maybe_limit_plds(
                ctx=ctx,
                spec=ctx_meta.get('pld_spec'),
            ),
        ):
            # NOTE: this in an implicit runtime nursery used to,
            # - start overrun queuing tasks when as well as
            # for cancellation of the scope opened by the user.
            ctx._scope_nursery: trio.Nursery = tn
            ctx._scope: trio.CancelScope = tn.cancel_scope

            # XXX NOTE since `._scope` is NOT set BEFORE we retreive the
            # `Started`-msg any cancellation triggered
            # in `._maybe_cancel_and_set_remote_error()` will
            # NOT actually cancel the below line!
            # -> it's expected that if there is an error in this phase of
            # the dialog, the `Error` msg should be raised from the `msg`
            # handling block below.
            try:
                started_msg, first = await ctx._pld_rx.recv_msg(
                    ipc=ctx,
                    expect_msg=Started,
                    passthrough_non_pld_msgs=False,
                    hide_tb=hide_tb,
                )
            except trio.Cancelled as taskc:
                ctx_cs: trio.CancelScope = ctx._scope
                if not ctx_cs.cancel_called:
                    raise

                # from .devx import pause
                # await pause(shield=True)

                log.cancel(
                    'IPC ctx was cancelled during "child" task sync due to\n\n'
                    f'{ctx.maybe_error}\n'
                )
                # OW if the ctx's scope was cancelled manually,
                # likely the `Context` was cancelled via a call to
                # `._maybe_cancel_and_set_remote_error()` so ensure
                # we raise the underlying `._remote_error` directly
                # instead of bubbling that taskc.
                ctx.maybe_raise(
                    # mask the above taskc from the tb
                    from_src_exc=None,
                    hide_tb=hide_tb,
                )

                # OW, some other unexpected cancel condition
                # that should prolly never happen right?
                raise InternalError(
                    'Invalid cancellation during IPC ctx sync phase?\n'
                ) from taskc

            ctx._started_called: bool = True
            ctx._started_msg: bool = started_msg
            ctx._started_pld: bool = first

            # deliver context ref and `.started()` msg payload value
            # in `__aenter__` tuple.
            yield ctx, first

            # ??TODO??: do we still want to consider this or is
            # the `else:` block handling via a `.wait_for_result()`
            # call below enough??
            #
            # -[ ] pretty sure `.wait_for_result()` internals do the
            # same as our ctxc handler below so it ended up
            # being same (repeated?) behaviour, but ideally we
            # wouldn't have that duplication either by somehow
            # factoring the `.wait_for_result()` handler impl in a way
            # that we can re-use it around the `yield` ^ here
            # or vice versa?
            #
            # maybe TODO NOTE: between the caller exiting and
            # arriving here the far end may have sent a ctxc-msg or
            # other error, so the quetion is whether we should check
            # for it here immediately and maybe raise so as to engage
            # the ctxc handling block below ????
            #
            # self.maybe_raise()

            # when in allow_overruns mode there may be
            # lingering overflow sender tasks remaining?
            if tn.child_tasks:
                # XXX: ensure we are in overrun state
                # with ``._allow_overruns=True`` bc otherwise
                # there should be no tasks in this nursery!
                if (
                    not ctx._allow_overruns
                    or len(tn.child_tasks) > 1
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
        ctx._local_error: BaseException = scope_err
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
        # await _debug.pause()

        # CASE 2: context was cancelled by local task calling
        # `.cancel()`, we don't raise and the exit block should
        # finish silently.
        if (
            ctx._cancel_called
            and
            ctxc is ctx._remote_error
            and
            ctxc.canceller == portal.actor.uid
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
        #    `.wait_for_result()` or `MsgStream.receive()`
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
        ctx._local_error: BaseException = scope_err

        # XXX: ALWAYS request the context to CANCEL ON any ERROR.
        # NOTE: `Context.cancel()` is conversely NEVER CALLED in
        # the `ContextCancelled` "self cancellation absorbed" case
        # handled in the block above ^^^ !!
        # await _debug.pause()
        # log.cancel(
        match scope_err:
            case trio.Cancelled:
                logmeth = log.cancel

            # XXX explicitly report on any non-graceful-taskc cases
            case _:
                logmeth = log.exception

        logmeth(
            f'ctx {ctx.side!r}-side exited with {ctx.repr_outcome()}\n'
        )

        if debug_mode():
            # async with _debug.acquire_debug_lock(portal.actor.uid):
            #     pass
            # TODO: factor ^ into below for non-root cases?
            #
            from .devx._debug import maybe_wait_for_debugger
            was_acquired: bool = await maybe_wait_for_debugger(
                # header_msg=(
                #     'Delaying `ctx.cancel()` until debug lock '
                #     'acquired..\n'
                # ),
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
                    f'task: {ctx.cid}\n'
                    f'actor: {uid}'
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
            # `Context.wait_for_result()` will ALWAYS raise
            # a `ContextCancelled` (via an embedded call to
            # `Context._maybe_raise_remote_err()`) IFF
            # a `Context._remote_error` was set by the runtime
            # via a call to
            # `Context._maybe_cancel_and_set_remote_error()`.
            # As per `Context._deliver_msg()`, that error IS
            # ALWAYS SET any time "callee" side fails and causes "caller
            # side" cancellation via a `ContextCancelled` here.
            try:
                result_or_err: Exception|Any = await ctx.wait_for_result()
            except BaseException as berr:
                # on normal teardown, if we get some error
                # raised in `Context.wait_for_result()` we still want to
                # save that error on the ctx's state to
                # determine things like `.cancelled_caught` for
                # cases where there was remote cancellation but
                # this task didn't know until final teardown
                # / value collection.
                scope_err = berr
                ctx._local_error: BaseException = scope_err
                raise

            # yes this worx!
            # from .devx import _debug
            # await _debug.pause()

            # an exception type boxed in a `RemoteActorError`
            # is returned (meaning it was obvi not raised)
            # that we want to log-report on.
            match result_or_err:
                case ContextCancelled() as ctxc:
                    log.cancel(ctxc.tb_str)

                case RemoteActorError() as rae:
                    log.exception(
                        'Context remotely errored!\n'
                        f'<= peer: {uid}\n'
                        f'  |_ {nsf}()\n\n'

                        f'{rae.tb_str}'
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
        # ``Actor._deliver_ctx_payload()`` the msg will be discarded and in
        # the case where that msg is global debugger unlock (via
        # a "stop" msg for a stream), this can result in a deadlock
        # where the root is waiting on the lock to clear but the
        # child has already cleared it and clobbered IPC.
        if debug_mode():
            from .devx._debug import maybe_wait_for_debugger
            await maybe_wait_for_debugger()

        # though it should be impossible for any tasks
        # operating *in* this scope to have survived
        # we tear down the runtime feeder chan last
        # to avoid premature stream clobbers.
        if (
            (rxchan := ctx._rx_chan)

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
            and
            not rxchan._closed
        ):
            # XXX NOTE XXX: and again as per above, we mask any
            # `trio.Cancelled` raised here so as to NOT mask
            # out any exception group or legit (remote) ctx
            # error that sourced from the remote task or its
            # runtime.
            #
            # NOTE: further, this should be the only place the
            # underlying feeder channel is
            # once-forever-and-only-CLOSED!
            with trio.CancelScope(shield=True):
                await ctx._rx_chan.aclose()

        # XXX: we always raise remote errors locally and
        # generally speaking mask runtime-machinery related
        # multi-`trio.Cancelled`s. As such, any `scope_error`
        # which was the underlying cause of this context's exit
        # should be stored as the `Context._local_error` and
        # used in determining `Context.cancelled_caught: bool`.
        if scope_err is not None:
            # sanity, tho can remove?
            assert ctx._local_error is scope_err
            # ctx._local_error: BaseException = scope_err
            # etype: Type[BaseException] = type(scope_err)

            # CASE 2
            if (
                ctx._cancel_called
                and ctx.cancel_acked
            ):
                log.cancel(
                    f'Context cancelled by local {ctx.side!r}-side task\n'
                    f'c)>\n'
                    f' |_{ctx._task}\n\n'
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
                    f'Context terminated due to {ctx.side!r}-side\n\n'
                    # TODO: do an x)> on err and c)> only for ctxc?
                    f'c)> {outcome_str}\n'
                    f' |_{ctx.repr_rpc}\n'
                )

        # FINALLY, remove the context from runtime tracking and
        # exit!
        log.runtime(
        # log.cancel(
            f'De-allocating IPC ctx opened with {ctx.side!r} peer \n'
            f'uid: {uid}\n'
            f'cid: {ctx.cid}\n'
        )
        portal.actor._contexts.pop(
            (uid, ctx.cid),
            None,
        )

        # XXX revert to prior IPC-task-ctx scope
        _ctxvar_Context.reset(prior_ctx_tok)


def mk_context(
    chan: Channel,
    cid: str,
    nsf: NamespacePath,

    msg_buffer_size: int = 2**6,
    pld_spec: Union[Type] = Any,

    **kwargs,

) -> Context:
    '''
    Internal factory to create an inter-actor task ``Context``.

    This is called by internals and should generally never be called
    by user code.

    '''
    send_chan: trio.MemorySendChannel
    recv_chan: trio.MemoryReceiveChannel
    send_chan, recv_chan = trio.open_memory_channel(msg_buffer_size)

    # TODO: only scan caller-info if log level so high!
    from .devx._frame_stack import find_caller_info
    caller_info: CallerInfo|None = find_caller_info()

    pld_rx = msgops.PldRx(
        _pld_dec=msgops._def_any_pldec,
    )

    ctx = Context(
        chan=chan,
        cid=cid,
        _actor=current_actor(),
        _send_chan=send_chan,
        _rx_chan=recv_chan,
        _pld_rx=pld_rx,
        _nsf=nsf,
        _task=trio.lowlevel.current_task(),
        _caller_info=caller_info,
        **kwargs,
    )
    ctx._result = Unresolved
    return ctx


# TODO: use the new type-parameters to annotate this in 3.13?
# -[ ] https://peps.python.org/pep-0718/#unknown-types
# -[ ] allow for `pld_spec` input(s) ideally breaking down,
# |_ `start: ParameterSpec`,
# |_ `started: TypeAlias`,
# |_ `yields: TypeAlias`,
# |_ `return: TypeAlias`,
# |_ `invalid_policy: str|Callable` ?
# -[ ] prolly implement the `@acm` wrapper using
#     a `contextlib.ContextDecorator`, i guess not if
#     we don't need an `__aexit__` block right?
#  |_ de hecho, @acm can already be used as a decorator as of 3.10
#     but i dunno how that's gonna play with `trio.Nursery.start[_soon]()`
#  |_ https://docs.python.org/3/library/contextlib.html#using-a-context-manager-as-a-function-decorator
#
def context(
    func: Callable|None = None,

    *,

    # must be named!
    pld_spec: Union[Type]|TypeAlias = Any,
    dec_hook: Callable|None = None,
    enc_hook: Callable|None = None,

) -> Callable:
    '''
    Mark an async function as an SC-supervised, inter-`Actor`, RPC
    scheduled child-side `Task`, IPC endpoint otherwise
    known more colloquially as a (RPC) "context".

    Functions annotated the fundamental IPC endpoint type offered by
    `tractor`.

    '''
    # XXX for the `@context(pld_spec=MyMsg|None)` case
    if func is None:
        return partial(
            context,
            pld_spec=pld_spec,
            dec_hook=dec_hook,
            enc_hook=enc_hook,
        )

    # TODO: from this, enforcing a `Start.sig` type
    # check when invoking RPC tasks by ensuring the input
    # args validate against the endpoint def.
    sig: inspect.Signature = inspect.signature(func)
    # params: inspect.Parameters = sig.parameters

    # https://docs.python.org/3/library/inspect.html#inspect.get_annotations
    annots: dict[str, Type] = inspect.get_annotations(
        func,
        eval_str=True,
    )
    name: str
    param: Type
    for name, param in annots.items():
        if (
            param is Context
            or (
                isinstance(param, UnionType)
                and
                Context in param.__args__
            )
        ):
            ctx_var_name: str = name
            break
    else:
        raise TypeError(
            'At least one (normally the first) argument to the `@context` function '
            f'{func.__name__!r} must be typed as `tractor.Context`, for ex,\n\n'
            f'`ctx: tractor.Context`\n'
        )

    # TODO: apply whatever solution ``mypy`` ends up picking for this:
    # https://github.com/python/mypy/issues/2087#issuecomment-769266912
    # func._tractor_context_function = True  # type: ignore
    func._tractor_context_meta: dict[str, Any] = {
        'ctx_var_name': ctx_var_name,
        # `msgspec` related settings
        'pld_spec': pld_spec,
        'enc_hook': enc_hook,
        'dec_hook': dec_hook,

        # TODO: eventually we need to "signature-check" with these
        # vs. the `Start` msg fields!
        # => this would allow for TPC endpoint argument-type-spec
        # limiting and we could then error on
        # invalid inputs passed to `.open_context(rpc_ep, arg0='blah')`
        'sig': sig,
    }
    return func
