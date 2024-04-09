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
from msgspec import (
    structs,
    defstruct,
)

from tractor._state import current_actor
from tractor.log import get_logger
from tractor.msg import (
    Error,
    Msg,
    Stop,
    Yield,
    pretty_struct,
    types as msgtypes,
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


# NOTE: more or less should be close to these:
# 'boxed_type',
# 'src_type',
# 'src_uid',
# 'canceller',
# 'sender',
# TODO: format this better if we're going to include it.
# 'relay_path',
#
_ipcmsg_keys: list[str] = [
    fi.name
    for fi, k, v
    in pretty_struct.iter_fields(Error)

]

_body_fields: list[str] = list(
    set(_ipcmsg_keys)

    # NOTE: don't show fields that either don't provide
    # any extra useful info or that are already shown
    # as part of `.__repr__()` output.
    - {
        'src_type_str',
        'boxed_type_str',
        'tb_str',
        'relay_path',
        '_msg_dict',
        'cid',
    }
)


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


def pformat_boxed_tb(
    tb_str: str,
    fields_str: str|None = None,
    field_prefix: str = ' |_',
    indent: str = ' '*2
) -> str:
    if (
        fields_str
        and
        field_prefix
    ):
        fields: str = textwrap.indent(
            fields_str,
            # prefix=' '*2,
            # prefix=' |_',
            prefix=field_prefix,
        )
    else:
        fields = fields_str or ''

    # body_indent: str = len(field_prefix) * ' '
    body: str = (

        # orig
        # f'  |\n'
        # f'   ------ - ------\n\n'
        # f'{tb_str}\n'
        # f'   ------ - ------\n'
        # f' _|\n'

        f'|\n'
        f' ------ - ------\n\n'
        f'{tb_str}\n'
        f'  ------ - ------\n'
        f'_|\n'
    )
    if len(indent):
        body: str = textwrap.indent(
            body,
            # prefix=body_indent,
            prefix=indent,
        )

    return (
        fields
        +
        body
    )


def pack_from_raise(
    local_err: (
        ContextCancelled
        |StreamOverrun
        |MsgTypeError
    ),
    cid: str,

    **rae_fields,

) -> Error:
    '''
    Raise the provided `RemoteActorError` subtype exception
    instance locally to get a traceback and pack it into an IPC
    `Error`-msg using `pack_error()` to extract the tb info.

    '''
    try:
        raise local_err
    except type(local_err) as local_err:
        err_msg: dict[str, dict] = pack_error(
            local_err,
            cid=cid,
            **rae_fields,
        )
        return err_msg


# TODO: better compat with IPC msg structs?
# -[ ] rename to just `RemoteError` like in `mp.manager`?
# -[ ] make a `Struct`-subtype by using the .__post_init__()`?
#  https://jcristharif.com/msgspec/structs.html#post-init-processing
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
        # 'relay_path',
    ]
    extra_body_fields: list[str] = [
        'cid',
        'boxed_type',
    ]

    def __init__(
        self,
        message: str,
        ipc_msg: Error|None = None,
        boxed_type: Type[BaseException]|None = None,

        # NOTE: only provided by subtypes (ctxc and overruns)
        # wishing to both manually instantiate and add field
        # values defined on `Error` without having to construct an
        # `Error()` before the exception is processed by
        # `pack_error()`.
        #
        # TODO: a better way to support this without the extra
        # private `._extra_msgdata`?
        # -[ ] ctxc constructed inside `._rpc._invoke()` L:638
        # -[ ] overrun @ `._context.Context._deliver_msg()` L:1958
        **extra_msgdata,

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
        self._boxed_type: BaseException = boxed_type
        self._src_type: BaseException|None = None
        self._ipc_msg: Error|None = ipc_msg

        if (
            extra_msgdata
            and ipc_msg
        ):
            # XXX mutate the orig msg directly from
            # manually provided input params.
            for k, v in extra_msgdata.items():
                setattr(
                    self._ipc_msg,
                    k,
                    v,
                )
        else:
            self._extra_msgdata = extra_msgdata

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
            boxed_type_str: str = type(boxed_type).__name__
            if (
                ipc_msg
                and not self._ipc_msg.boxed_type_str
            ):
                self._ipc_msg.boxed_type_str = boxed_type_str
                assert self.boxed_type_str == self._ipc_msg.boxed_type_str

            else:
                self._extra_msgdata['boxed_type_str'] = boxed_type_str

            assert self.boxed_type is boxed_type

    @property
    def ipc_msg(self) -> pretty_struct.Struct:
        '''
        Re-render the underlying `._ipc_msg: Msg` as
        a `pretty_struct.Struct` for introspection such that the
        returned value is a read-only copy of the original.

        '''
        if self._ipc_msg is None:
            return None

        msg_type: Msg = type(self._ipc_msg)
        fields: dict[str, Any] = {
            k: v for _, k, v in
            pretty_struct.iter_fields(self._ipc_msg)
        }
        return defstruct(
            msg_type.__name__,
            fields=fields.keys(),
            bases=(msg_type, pretty_struct.Struct),
        )(**fields)

    @property
    def msgdata(self) -> dict[str, Any]:
        '''
        The (remote) error data provided by a merge of the
        `._ipc_msg: Error` msg and any input `._extra_msgdata: dict`
        (provided by subtypes via `.__init__()`).

        '''
        msgdata: dict = (
            structs.asdict(self._ipc_msg)
            if self._ipc_msg
            else {}
        )
        return self._extra_msgdata | msgdata

    @property
    def src_type_str(self) -> str:
        '''
        String-name of the source error's type.

        This should be the same as `.boxed_type_str` when unpacked
        at the first relay/hop's receiving actor.

        '''
        return self._ipc_msg.src_type_str

    @property
    def src_type(self) -> str:
        '''
        Error type raised by original remote faulting actor.

        '''
        if self._src_type is None:
            self._src_type = get_err_type(
                self._ipc_msg.src_type_str
            )

        return self._src_type

    @property
    def boxed_type_str(self) -> str:
        '''
        String-name of the (last hop's) boxed error type.

        '''
        return self._ipc_msg.boxed_type_str

    @property
    def boxed_type(self) -> str:
        '''
        Error type boxed by last actor IPC hop.

        '''
        if self._boxed_type is None:
            self._boxed_type = get_err_type(
                self._ipc_msg.boxed_type_str
            )

        return self._boxed_type

    @property
    def relay_path(self) -> list[tuple]:
        '''
        Return the list of actors which consecutively relayed
        a boxed `RemoteActorError` the src error up until THIS
        actor's hop.

        NOTE: a `list` field with the same name is expected to be
        passed/updated in `.ipc_msg`.

        '''
        return self._ipc_msg.relay_path

    @property
    def relay_uid(self) -> tuple[str, str]|None:
        return tuple(
            self._ipc_msg.relay_path[-1]
        )

    @property
    def src_uid(self) -> tuple[str, str]|None:
        if src_uid := (
            self._ipc_msg.src_uid
        ):
            return tuple(src_uid)
        # TODO: use path lookup instead?
        # return tuple(
        #     self._ipc_msg.relay_path[0]
        # )

    @property
    def tb_str(
        self,
        indent: str = '',
    ) -> str:
        remote_tb: str = ''

        if self._ipc_msg:
            remote_tb: str = self._ipc_msg.tb_str
        else:
            remote_tb = self.msgdata.get('tb_str')

        return textwrap.indent(
            remote_tb or '',
            prefix=indent,
        )

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
                getattr(
                    self._ipc_msg,
                    key,
                    None,
                )
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
            _body_fields
            +
            self.extra_body_fields,
        )
        body: str = pformat_boxed_tb(
            tb_str=self.tb_str,
            fields_str=fields,
            field_prefix=' |_',
            indent=' ',  # no indent?
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


class ContextCancelled(RemoteActorError):
    '''
    Inter-actor task context was cancelled by either a call to
    ``Portal.cancel_actor()`` or ``Context.cancel()``.

    '''
    reprol_fields: list[str] = [
        'canceller',
    ]
    extra_body_fields: list[str] = [
        'cid',
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
        value: tuple[str, str]|None = self._ipc_msg.canceller
        if value:
            return tuple(value)

        log.warning(
            'IPC Context cancelled without a requesting actor?\n'
            'Maybe the IPC transport ended abruptly?\n\n'
            f'{self}'
        )

    # TODO: to make `.__repr__()` work uniformly?
    # src_actor_uid = canceller


class MsgTypeError(
    RemoteActorError,
):
    '''
    Equivalent of a runtime `TypeError` for IPC dialogs.

    Raise when any IPC wire-message is decoded to have invalid
    field values (due to type) or for other `MsgCodec` related
    violations such as having no extension-type for a field with
    a custom type but no `enc/dec_hook()` support.

    Can be raised on the send or recv side of an IPC `Channel`
    depending on the particular msg.

    Msgs which cause this to be raised on the `.send()` side (aka
    in the "ctl" dialog phase) include:
    - `Start`
    - `Started`
    - `Return`

    Those which cause it on on the `.recv()` side (aka the "nasty
    streaming" dialog phase) are:
    - `Yield`
    - TODO: any embedded `.pld` type defined by user code?

    Normally the source of an error is re-raised from some `.msg._codec`
    decode which itself raises in a backend interchange
    lib (eg. a `msgspec.ValidationError`).

    '''
    reprol_fields: list[str] = [
        'ipc_msg',
    ]
    extra_body_fields: list[str] = [
        'cid',
        'payload_msg',
    ]

    @property
    def msg_dict(self) -> dict[str, Any]:
        '''
        If the underlying IPC `Msg` was received from a remote
        actor but was unable to be decoded to a native
        `Yield`|`Started`|`Return` struct, the interchange backend
        native format decoder can be used to stash a `dict`
        version for introspection by the invalidating RPC task.

        '''
        return self.msgdata.get('_msg_dict')

    @property
    def payload_msg(self) -> Msg|None:
        '''
        Attempt to construct what would have been the original
        `Msg`-with-payload subtype (i.e. an instance from the set
        of msgs in `.msg.types._payload_msgs`) which failed
        validation.

        '''
        msg_dict: dict = self.msg_dict.copy()
        name: str = msg_dict.pop('msg_type')
        msg_type: Msg = getattr(
            msgtypes,
            name,
            Msg,
        )
        return msg_type(**msg_dict)

    @property
    def cid(self) -> str:
        # pre-packed using `.from_decode()` constructor
        return self.msgdata.get('cid')

    @classmethod
    def from_decode(
        cls,
        message: str,
        msgdict: dict,

    ) -> MsgTypeError:
        return cls(
            message=message,

            # NOTE: original "vanilla decode" of the msg-bytes
            # is placed inside a value readable from
            # `.msgdata['_msg_dict']`
            _msg_dict=msgdict,

            # expand and pack all RAE compat fields
            # into the `._extra_msgdata` aux `dict`.
            **{
                k: v
                for k, v in msgdict.items()
                if k in _ipcmsg_keys
            },
        )


class StreamOverrun(
    RemoteActorError,
    trio.TooSlowError,
):
    reprol_fields: list[str] = [
        'sender',
    ]
    '''
    This stream was overrun by its sender and can be optionally
    handled by app code using `MsgStream.send()/.receive()`.

    '''
    @property
    def sender(self) -> tuple[str, str] | None:
        value = self._ipc_msg.sender
        if value:
            return tuple(value)


# class InternalActorError(RemoteActorError):
#     '''
#     Boxed (Remote) internal `tractor` error indicating failure of some
#     primitive, machinery state or lowlevel task that should never
#     occur.

#     '''


class TransportClosed(trio.ClosedResourceError):
    "Underlying channel transport was closed prior to use"


class NoResult(RuntimeError):
    "No final result is expected for this actor"


class ModuleNotExposed(ModuleNotFoundError):
    "The requested module is not exposed for RPC"


class NoRuntime(RuntimeError):
    "The root actor has not been initialized yet"



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


def pack_error(
    exc: BaseException|RemoteActorError,

    tb: str|None = None,
    cid: str|None = None,
    src_uid: tuple[str, str]|None = None,

) -> Error:
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

    # an onion/inception we need to pack as a nested and relayed
    # remotely boxed error.
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
        error_msg['src_uid'] = src_uid or our_uid
        error_msg['src_type_str'] =  type(exc).__name__
        error_msg['boxed_type_str'] = type(exc).__name__

    # XXX alawys append us the last relay in error propagation path
    error_msg.setdefault(
        'relay_path',
        [],
    ).append(our_uid)

    # XXX NOTE: always ensure the traceback-str is from the
    # locally raised error (**not** the prior relay's boxed
    # content's in `._ipc_msg.tb_str`).
    error_msg['tb_str'] = tb_str

    if cid is not None:
        error_msg['cid'] = cid

    return Error(**error_msg)


def unpack_error(
    msg: Error,

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

    if not isinstance(msg, Error):
        return None

    # retrieve the remote error's encoded details from fields
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

    elif boxed_type_str == 'MsgTypeError':
        box_type = MsgTypeError
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

    exc = box_type(
        message,
        ipc_msg=msg,
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
