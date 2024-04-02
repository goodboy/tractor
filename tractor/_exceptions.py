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
Our classy exception set.

'''
from __future__ import annotations
import builtins
import importlib
from pprint import pformat
from typing import (
    Any,
    Type,
    TYPE_CHECKING,
)
import textwrap
import traceback

import trio
from msgspec import structs

from tractor._state import current_actor
from tractor.log import get_logger
from tractor.msg import (
    Error,
    Msg,
    Stop,
    Yield,
)

if TYPE_CHECKING:
    from ._context import Context
    from .log import StackLevelAdapter
    from ._stream import MsgStream
    from ._ipc import Channel

log = get_logger('tractor')

_this_mod = importlib.import_module(__name__)


class ActorFailure(Exception):
    "General actor failure"


class InternalError(RuntimeError):
    '''
    Entirely unexpected internal machinery error indicating
    a completely invalid state or interface.

    '''

_body_fields: list[str] = [
    'boxed_type',
    'src_type',
    # TODO: format this better if we're going to include it.
    # 'relay_path',
    'src_uid',

    # only in sub-types
    'canceller',
    'sender',
]

_msgdata_keys: list[str] = [
    'boxed_type_str',
] + _body_fields


def get_err_type(type_name: str) -> BaseException|None:
    '''
    Look up an exception type by name from the set of locally
    known namespaces:

    - `builtins`
    - `tractor._exceptions`
    - `trio`

    '''
    for ns in [
        builtins,
        _this_mod,
        trio,
    ]:
        if type_ref := getattr(
            ns,
            type_name,
            False,
        ):
            return type_ref


# TODO: rename to just `RemoteError`?
class RemoteActorError(Exception):
    '''
    A box(ing) type which bundles a remote actor `BaseException` for
    (near identical, and only if possible,) local object/instance
    re-construction in the local process memory domain.

    Normally each instance is expected to be constructed from
    a special "error" IPC msg sent by some remote actor-runtime.

    '''
    reprol_fields: list[str] = [
        'src_uid',
        'relay_path',
    ]

    def __init__(
        self,
        message: str,
        boxed_type: Type[BaseException]|None = None,
        **msgdata

    ) -> None:
        super().__init__(message)

        # TODO: maybe a better name?
        # - .errtype
        # - .retype
        # - .boxed_errtype
        # - .boxed_type
        # - .remote_type
        # also pertains to our long long oustanding issue XD
        # https://github.com/goodboy/tractor/issues/5
        #
        # TODO: always set ._boxed_type` as `None` by default
        # and instead render if from `.boxed_type_str`?
        self._boxed_type: BaseException = boxed_type
        self._src_type: BaseException|None = None

        # TODO: make this a `.errmsg: Error` throughout?
        self.msgdata: dict[str, Any] = msgdata

        # TODO: mask out eventually or place in `pack_error()`
        # pre-`return` lines?
        # sanity on inceptions
        if boxed_type is RemoteActorError:
            assert self.src_type_str != 'RemoteActorError'
            assert self.src_uid not in self.relay_path

        # ensure type-str matches and round-tripping from that
        # str results in same error type.
        #
        # TODO NOTE: this is currently exclusively for the
        # `ContextCancelled(boxed_type=trio.Cancelled)` case as is
        # used inside `._rpc._invoke()` atm though probably we
        # should better emphasize that special (one off?) case
        # either by customizing `ContextCancelled.__init__()` or
        # through a special factor func?
        elif boxed_type:
            if not self.msgdata.get('boxed_type_str'):
                self.msgdata['boxed_type_str'] = str(
                    type(boxed_type).__name__
                )

            assert self.boxed_type_str == self.msgdata['boxed_type_str']
            assert self.boxed_type is boxed_type

    @property
    def src_type_str(self) -> str:
        '''
        String-name of the source error's type.

        This should be the same as `.boxed_type_str` when unpacked
        at the first relay/hop's receiving actor.

        '''
        return self.msgdata['src_type_str']

    @property
    def src_type(self) -> str:
        '''
        Error type raised by original remote faulting actor.

        '''
        if self._src_type is None:
            self._src_type = get_err_type(
                self.msgdata['src_type_str']
            )

        return self._src_type

    @property
    def boxed_type_str(self) -> str:
        '''
        String-name of the (last hop's) boxed error type.

        '''
        return self.msgdata['boxed_type_str']

    @property
    def boxed_type(self) -> str:
        '''
        Error type boxed by last actor IPC hop.

        '''
        if self._boxed_type is None:
            self._boxed_type = get_err_type(
                self.msgdata['boxed_type_str']
            )

        return self._boxed_type

    @property
    def relay_path(self) -> list[tuple]:
        '''
        Return the list of actors which consecutively relayed
        a boxed `RemoteActorError` the src error up until THIS
        actor's hop.

        NOTE: a `list` field with the same name is expected to be
        passed/updated in `.msgdata`.

        '''
        return self.msgdata['relay_path']

    @property
    def relay_uid(self) -> tuple[str, str]|None:
        return tuple(
            self.msgdata['relay_path'][-1]
        )

    @property
    def src_uid(self) -> tuple[str, str]|None:
        if src_uid := (
            self.msgdata.get('src_uid')
        ):
            return tuple(src_uid)
        # TODO: use path lookup instead?
        # return tuple(
        #     self.msgdata['relay_path'][0]
        # )

    @property
    def tb_str(
        self,
        indent: str = ' '*3,
    ) -> str:
        if remote_tb := self.msgdata.get('tb_str'):
            return textwrap.indent(
                remote_tb,
                prefix=indent,
            )

        return ''

    def _mk_fields_str(
        self,
        fields: list[str],
        end_char: str = '\n',
    ) -> str:
        _repr: str = ''
        for key in fields:
            val: Any|None = (
                getattr(self, key, None)
                or
                self.msgdata.get(key)
            )
            # TODO: for `.relay_path` on multiline?
            # if not isinstance(val, str):
            #     val_str = pformat(val)
            # else:
            val_str: str = repr(val)

            if val:
                _repr += f'{key}={val_str}{end_char}'

        return _repr

    def reprol(self) -> str:
        '''
        Represent this error for "one line" display, like in
        a field of our `Context.__repr__()` output.

        '''
        # TODO: use this matryoshka emjoi XD
        # => 🪆
        reprol_str: str = f'{type(self).__name__}('
        _repr: str = self._mk_fields_str(
            self.reprol_fields,
            end_char=' ',
        )
        return (
            reprol_str
            +
            _repr
        )

    def __repr__(self) -> str:
        '''
        Nicely formatted boxed error meta data + traceback.

        '''
        fields: str = self._mk_fields_str(
            _body_fields,
        )
        fields: str = textwrap.indent(
            fields,
            # prefix=' '*2,
            prefix=' |_',
        )
        indent: str = ''*1
        body: str = (
            f'{fields}'
            f'  |\n'
            f'   ------ - ------\n\n'
            f'{self.tb_str}\n'
            f'   ------ - ------\n'
            f' _|\n'
        )
        if indent:
            body: str = textwrap.indent(
                body,
                prefix=indent,
            )
        return (
            f'<{type(self).__name__}(\n'
            f'{body}'
            ')>'
        )

    def unwrap(
        self,
    ) -> BaseException:
        '''
        Unpack the inner-most source error from it's original IPC msg data.

        We attempt to reconstruct (as best as we can) the original
        `Exception` from as it would have been raised in the
        failing actor's remote env.

        '''
        src_type_ref: Type[BaseException] = self.src_type
        if not src_type_ref:
            raise TypeError(
                'Failed to lookup src error type:\n'
                f'{self.src_type_str}'
            )

        # TODO: better tb insertion and all the fancier dunder
        # metadata stuff as per `.__context__` etc. and friends:
        # https://github.com/python-trio/trio/issues/611
        return src_type_ref(self.tb_str)

    # TODO: local recontruction of nested inception for a given
    # "hop" / relay-node in this error's relay_path?
    # => so would render a `RAE[RAE[RAE[Exception]]]` instance
    #   with all inner errors unpacked?
    # -[ ] if this is useful shouldn't be too hard to impl right?
    # def unbox(self) -> BaseException:
    #     '''
    #     Unbox to the prior relays (aka last boxing actor's)
    #     inner error.

    #     '''
    #     if not self.relay_path:
    #         return self.unwrap()

    #     # TODO..
    #     # return self.boxed_type(
    #     #     boxed_type=get_type_ref(..
    #     raise NotImplementedError


class InternalActorError(RemoteActorError):
    '''
    (Remote) internal `tractor` error indicating failure of some
    primitive, machinery state or lowlevel task that should never
    occur.

    '''


class ContextCancelled(RemoteActorError):
    '''
    Inter-actor task context was cancelled by either a call to
    ``Portal.cancel_actor()`` or ``Context.cancel()``.

    '''
    reprol_fields: list[str] = [
        'canceller',
    ]
    @property
    def canceller(self) -> tuple[str, str]|None:
        '''
        Return the (maybe) `Actor.uid` for the requesting-author
        of this ctxc.

        Emit a warning msg when `.canceller` has not been set,
        which usually idicates that a `None` msg-loop setinel was
        sent before expected in the runtime. This can happen in
        a few situations:

        - (simulating) an IPC transport network outage
        - a (malicious) pkt sent specifically to cancel an actor's
          runtime non-gracefully without ensuring ongoing RPC tasks are 
          incrementally cancelled as is done with:
          `Actor`
          |_`.cancel()`
          |_`.cancel_soon()`
          |_`._cancel_task()`

        '''
        value = self.msgdata.get('canceller')
        if value:
            return tuple(value)

        log.warning(
            'IPC Context cancelled without a requesting actor?\n'
            'Maybe the IPC transport ended abruptly?\n\n'
            f'{self}'
        )

    # TODO: to make `.__repr__()` work uniformly?
    # src_actor_uid = canceller


class TransportClosed(trio.ClosedResourceError):
    "Underlying channel transport was closed prior to use"


class NoResult(RuntimeError):
    "No final result is expected for this actor"


class ModuleNotExposed(ModuleNotFoundError):
    "The requested module is not exposed for RPC"


class NoRuntime(RuntimeError):
    "The root actor has not been initialized yet"


class StreamOverrun(
    RemoteActorError,
    trio.TooSlowError,
):
    reprol_fields: list[str] = [
        'sender',
    ]
    '''
    This stream was overrun by sender

    '''
    @property
    def sender(self) -> tuple[str, str] | None:
        value = self.msgdata.get('sender')
        if value:
            return tuple(value)


class AsyncioCancelled(Exception):
    '''
    Asyncio cancelled translation (non-base) error
    for use with the ``to_asyncio`` module
    to be raised in the ``trio`` side task

    '''

class MessagingError(Exception):
    '''
    IPC related msg (typing), transaction (ordering) or dialog
    handling error.

    '''


class MsgTypeError(MessagingError):
    '''
    Equivalent of a `TypeError` for an IPC wire-message
    due to an invalid field value (type).

    Normally this is re-raised from some `.msg._codec`
    decode error raised by a backend interchange lib
    like `msgspec` or `pycapnproto`.

    '''


def pack_error(
    exc: BaseException|RemoteActorError,

    tb: str|None = None,
    cid: str|None = None,

) -> Error|dict[str, dict]:
    '''
    Create an "error message" which boxes a locally caught
    exception's meta-data and encodes it for wire transport via an
    IPC `Channel`; expected to be unpacked (and thus unboxed) on
    the receiver side using `unpack_error()` below.

    '''
    if tb:
        tb_str = ''.join(traceback.format_tb(tb))
    else:
        tb_str = traceback.format_exc()

    error_msg: dict[  # for IPC
        str,
        str | tuple[str, str]
    ] = {}
    our_uid: tuple = current_actor().uid

    if (
        isinstance(exc, RemoteActorError)
    ):
        error_msg.update(exc.msgdata)

    # an onion/inception we need to pack
    if (
        type(exc) is RemoteActorError
        and (boxed := exc.boxed_type)
        and boxed != RemoteActorError
    ):
        # sanity on source error (if needed when tweaking this)
        assert (src_type := exc.src_type) != RemoteActorError
        assert error_msg['src_type_str'] != 'RemoteActorError'
        assert error_msg['src_type_str'] == src_type.__name__
        assert error_msg['src_uid'] != our_uid

        # set the boxed type to be another boxed type thus
        # creating an "inception" when unpacked by
        # `unpack_error()` in another actor who gets "relayed"
        # this error Bo
        #
        # NOTE on WHY: since we are re-boxing and already
        # boxed src error, we want to overwrite the original
        # `boxed_type_str` and instead set it to the type of
        # the input `exc` type.
        error_msg['boxed_type_str'] = 'RemoteActorError'

    else:
        error_msg['src_uid'] = our_uid
        error_msg['src_type_str'] =  type(exc).__name__
        error_msg['boxed_type_str'] = type(exc).__name__

    # XXX alawys append us the last relay in error propagation path
    error_msg.setdefault(
        'relay_path',
        [],
    ).append(our_uid)

    # XXX NOTE: always ensure the traceback-str is from the
    # locally raised error (**not** the prior relay's boxed
    # content's `.msgdata`).
    error_msg['tb_str'] = tb_str

    # Error()
    # pkt: dict = {
    #     'error': error_msg,
    # }
    pkt: Error = Error(
        cid=cid,
        **error_msg,
        # TODO: just get rid of `.pld` on this msg?
    )
    # if cid:
    #     pkt['cid'] = cid

    return pkt


def unpack_error(
    msg: dict[str, Any]|Error,

    chan: Channel|None = None,
    box_type: RemoteActorError = RemoteActorError,

    hide_tb: bool = True,

) -> None|Exception:
    '''
    Unpack an 'error' message from the wire
    into a local `RemoteActorError` (subtype).

    NOTE: this routine DOES not RAISE the embedded remote error,
    which is the responsibilitiy of the caller.

    '''
    __tracebackhide__: bool = hide_tb

    error_dict: dict[str, dict]|None
    if not isinstance(msg, Error):
    # if (
    #     error_dict := msg.get('error')
    # ) is None:
        # no error field, nothing to unpack.
        return None

    # retrieve the remote error's msg encoded details
    # tb_str: str = error_dict.get('tb_str', '')
    tb_str: str = msg.tb_str
    message: str = (
        f'{chan.uid}\n'
        +
        tb_str
    )

    # try to lookup a suitable error type from the local runtime
    # env then use it to construct a local instance.
    # boxed_type_str: str = error_dict['boxed_type_str']
    boxed_type_str: str = msg.boxed_type_str
    boxed_type: Type[BaseException] = get_err_type(boxed_type_str)

    if boxed_type_str == 'ContextCancelled':
        box_type = ContextCancelled
        assert boxed_type is box_type

    # TODO: already included by `_this_mod` in else loop right?
    #
    # we have an inception/onion-error so ensure
    # we include the relay_path info and the
    # original source error.
    elif boxed_type_str == 'RemoteActorError':
        assert boxed_type is RemoteActorError
        # assert len(error_dict['relay_path']) >= 1
        assert len(msg.relay_path) >= 1

    # TODO: mk RAE just take the `Error` instance directly?
    error_dict: dict = structs.asdict(msg)

    exc = box_type(
        message,
        **error_dict,
    )

    return exc


def is_multi_cancelled(exc: BaseException) -> bool:
    '''
    Predicate to determine if a possible ``BaseExceptionGroup`` contains
    only ``trio.Cancelled`` sub-exceptions (and is likely the result of
    cancelling a collection of subtasks.

    '''
    # if isinstance(exc, eg.BaseExceptionGroup):
    if isinstance(exc, BaseExceptionGroup):
        return exc.subgroup(
            lambda exc: isinstance(exc, trio.Cancelled)
        ) is not None

    return False


def _raise_from_no_key_in_msg(
    ctx: Context,
    msg: Msg,
    src_err: KeyError,
    log: StackLevelAdapter,  # caller specific `log` obj

    expect_key: str = 'yield',
    expect_msg: str = Yield,
    stream: MsgStream | None = None,

    # allow "deeper" tbs when debugging B^o
    hide_tb: bool = True,

) -> bool:
    '''
    Raise an appopriate local error when a
    `MsgStream` msg arrives which does not
    contain the expected (at least under normal
    operation) `'yield'` field.

    `Context` and any embedded `MsgStream` termination,
    as well as remote task errors are handled in order
    of priority as:

    - any 'error' msg is re-boxed and raised locally as
      -> `RemoteActorError`|`ContextCancelled`

    - a `MsgStream` 'stop' msg is constructed, assigned
      and raised locally as -> `trio.EndOfChannel`

    - All other mis-keyed msgss (like say a "final result"
      'return' msg, normally delivered from `Context.result()`)
      are re-boxed inside a `MessagingError` with an explicit
      exc content describing the missing IPC-msg-key.

    '''
    __tracebackhide__: bool = hide_tb

    # an internal error should never get here
    try:
        cid: str = msg.cid
        # cid: str = msg['cid']
    # except KeyError as src_err:
    except AttributeError as src_err:
        raise MessagingError(
            f'IPC `Context` rx-ed msg without a ctx-id (cid)!?\n'
            f'cid: {cid}\n\n'

            f'{pformat(msg)}\n'
        ) from src_err

    # TODO: test that shows stream raising an expected error!!!

    # raise the error message in a boxed exception type!
    # if msg.get('error'):
    if isinstance(msg, Error):
    # match msg:
    #     case Error():
        raise unpack_error(
            msg,
            ctx.chan,
            hide_tb=hide_tb,

        ) from None

    # `MsgStream` termination msg.
    # TODO: does it make more sense to pack 
    # the stream._eoc outside this in the calleer always?
        # case Stop():
    elif (
        # msg.get('stop')
        isinstance(msg, Stop)
        or (
            stream
            and stream._eoc
        )
    ):
        log.debug(
            f'Context[{cid}] stream was stopped by remote side\n'
            f'cid: {cid}\n'
        )

        # TODO: if the a local task is already blocking on
        # a `Context.result()` and thus a `.receive()` on the
        # rx-chan, we close the chan and set state ensuring that
        # an eoc is raised!

        # XXX: this causes ``ReceiveChannel.__anext__()`` to
        # raise a ``StopAsyncIteration`` **and** in our catch
        # block below it will trigger ``.aclose()``.
        eoc = trio.EndOfChannel(
            f'Context stream ended due to msg:\n\n'
            f'{pformat(msg)}\n'
        )
        # XXX: important to set so that a new `.receive()`
        # call (likely by another task using a broadcast receiver)
        # doesn't accidentally pull the `return` message
        # value out of the underlying feed mem chan which is
        # destined for the `Context.result()` call during ctx-exit!
        stream._eoc: Exception = eoc

        # in case there already is some underlying remote error
        # that arrived which is probably the source of this stream
        # closure
        ctx.maybe_raise()

        raise eoc from src_err

    if (
        stream
        and stream._closed
    ):
        # TODO: our own error subtype?
        raise trio.ClosedResourceError(
            'This stream was closed'
        )

    # always re-raise the source error if no translation error case
    # is activated above.
    _type: str = 'Stream' if stream else 'Context'
    raise MessagingError(
        f"{_type} was expecting a '{expect_key.upper()}' message"
        " BUT received a non-error msg:\n"
        f'{pformat(msg)}'
    ) from src_err
