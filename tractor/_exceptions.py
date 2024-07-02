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
import sys
from types import (
    TracebackType,
)
from typing import (
    Any,
    Callable,
    Type,
    TYPE_CHECKING,
)
import textwrap
import traceback

import trio
from msgspec import (
    defstruct,
    msgpack,
    structs,
    ValidationError,
)

from tractor._state import current_actor
from tractor.log import get_logger
from tractor.msg import (
    Error,
    PayloadMsg,
    MsgType,
    MsgCodec,
    MsgDec,
    Stop,
    types as msgtypes,
)
from tractor.msg.pretty_struct import (
    iter_fields,
    Struct,
    pformat as struct_format,
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
    in iter_fields(Error)
]

_body_fields: list[str] = list(
    set(_ipcmsg_keys)

    # XXX NOTE: DON'T-SHOW-FIELDS
    # - don't provide any extra useful info or,
    # - are already shown as part of `.__repr__()` or,
    # - are sub-type specific.
    - {
        'src_type_str',
        'boxed_type_str',
        'tb_str',
        'relay_path',
        'cid',
        'message',

        # only ctxc should show it but `Error` does
        # have it as an optional field.
        'canceller',

        # only for MTEs and generally only used
        # when devving/testing/debugging.
        '_msg_dict',
        '_bad_msg',
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


def pack_from_raise(
    local_err: (
        ContextCancelled
        |StreamOverrun
        |MsgTypeError
    ),
    cid: str,
    hide_tb: bool = True,

    **rae_fields,

) -> Error:
    '''
    Raise the provided `RemoteActorError` subtype exception
    instance locally to get a traceback and pack it into an IPC
    `Error`-msg using `pack_error()` to extract the tb info.

    '''
    __tracebackhide__: bool = hide_tb
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
        # NOTE: we only show this on relayed errors (aka
        # "inceptions").
        'relay_uid',
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

        # for manual display without having to muck with `Exception.args`
        self._message: str = message
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
        self._extra_msgdata = extra_msgdata

        if (
            extra_msgdata
            and
            ipc_msg
        ):
            # XXX mutate the orig msg directly from
            # manually provided input params.
            for k, v in extra_msgdata.items():
                setattr(
                    self._ipc_msg,
                    k,
                    v,
                )

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
            boxed_type_str: str = boxed_type.__name__
            if (
                ipc_msg
                and
                self._ipc_msg.boxed_type_str != boxed_type_str
            ):
                self._ipc_msg.boxed_type_str = boxed_type_str
                assert self.boxed_type_str == self._ipc_msg.boxed_type_str

            # ensure any roundtripping evals to the input value
            assert self.boxed_type is boxed_type

    @property
    def message(self) -> str:
        '''
        Be explicit, instead of trying to read it from the the parent
        type's loosely defined `.args: tuple`:

        https://docs.python.org/3/library/exceptions.html#BaseException.args

        '''
        return self._message

    @property
    def ipc_msg(self) -> Struct:
        '''
        Re-render the underlying `._ipc_msg: MsgType` as
        a `pretty_struct.Struct` for introspection such that the
        returned value is a read-only copy of the original.

        '''
        if self._ipc_msg is None:
            return None

        msg_type: MsgType = type(self._ipc_msg)
        fields: dict[str, Any] = {
            k: v for _, k, v in
            iter_fields(self._ipc_msg)
        }
        return defstruct(
            msg_type.__name__,
            fields=fields.keys(),
            bases=(msg_type, Struct),
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
        return {
            k: v for k, v in self._extra_msgdata.items()
        } | msgdata

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

        When the error has only been relayed a single actor-hop
        this will be the same as the `.boxed_type`.

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
        bt: Type[BaseException] = self.boxed_type
        if bt:
            return str(bt.__name__)

        return ''

    @property
    def boxed_type(self) -> Type[BaseException]:
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
            if (
                key == 'relay_uid'
                and not self.is_inception()
            ):
                continue

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
        reprol_str: str = (
            f'{type(self).__name__}'  # type name
            f'[{self.boxed_type_str}]'  # parameterized by boxed type
        )

        _repr: str = self._mk_fields_str(
            self.reprol_fields,
            end_char=' ',
        )
        if _repr:
            reprol_str += '('  # init-style call

        return (
            reprol_str
            +
            _repr
        )

    def is_inception(self) -> bool:
        '''
        Predicate which determines if the shuttled error type
        is the same as the container error type; IOW is this
        an "error within and error" which points to some original
        source error that was relayed through multiple
        actor hops.

        Ex. a relayed remote error will generally be some form of
        `RemoteActorError[RemoteActorError]` with a `.src_type` which
        is not of that same type.

        '''
        # if a single hop boxed error it was not relayed
        # more then one hop directly from the src actor.
        if (
            self.boxed_type
            is
            self.src_type
        ):
            return False

        return True

    def pformat(
        self,
        with_type_header: bool = True,

    ) -> str:
        '''
        Format any boxed remote error by multi-line display of,

          - error's src or relay actor meta-data,
          - remote runtime env's traceback,

        With optional control over the format of,

          - whether the boxed traceback is ascii-decorated with
            a surrounding "box" annotating the embedded stack-trace.
          - if the error's type name should be added as margins
            around the field and tb content like:

            `<RemoteActorError(.. <<multi-line-content>> .. )>`

          - the placement of the `.message: str` (explicit equiv of
            `.args[0]`), either placed below the `.tb_str` or in the
            first line's header when the error is raised locally (since
            the type name is already implicitly shown by python).

        '''
        header: str = ''
        body: str = ''
        message: str = ''

        # XXX when the currently raised exception is this instance,
        # we do not ever use the "type header" style repr.
        is_being_raised: bool = False
        if (
            (exc := sys.exception())
            and
            exc is self
        ):
            is_being_raised: bool = True

        with_type_header: bool = (
            with_type_header
            and
            not is_being_raised
        )

        # <RemoteActorError( .. )> style
        if with_type_header:
            header: str = f'<{type(self).__name__}('

        if message := self._message:

            # split off the first line so, if needed, it isn't
            # indented the same like the "boxed content" which
            # since there is no `.tb_str` is just the `.message`.
            lines: list[str] = message.splitlines()
            first: str = lines[0]
            message: str = message.removeprefix(first)

            # with a type-style header we,
            # - have no special message "first line" extraction/handling
            # - place the message a space in from the header:
            #  `MsgTypeError( <message> ..`
            #                 ^-here
            # - indent the `.message` inside the type body.
            if with_type_header:
                first = f' {first} )>'

            message: str = textwrap.indent(
                message,
                prefix=' '*2,
            )
            message: str = first + message

        # IFF there is an embedded traceback-str we always
        # draw the ascii-box around it.
        if tb_str := self.tb_str:
            fields: str = self._mk_fields_str(
                _body_fields
                +
                self.extra_body_fields,
            )
            from tractor.devx import (
                pformat_boxed_tb,
            )
            body: str = pformat_boxed_tb(
                tb_str=tb_str,
                fields_str=fields,
                field_prefix=' |_',
                # ^- is so that it's placed like so,
                # just after <Type(
                #             |___ ..
                tb_body_indent=1,
            )

        tail = ''
        if (
            with_type_header
            and not message
        ):
            tail: str = '>'

        return (
            header
            +
            message
            +
            f'{body}'
            +
            tail
        )

    __repr__ = pformat

    # NOTE: apparently we need this so that
    # the full fields show in debugger tests?
    # |_ i guess `pexepect` relies on `str`-casing
    #    of output?
    def __str__(self) -> str:
        return self.pformat(
            with_type_header=False
        )

    def unwrap(
        self,
    ) -> BaseException:
        '''
        Unpack the inner-most source error from it's original IPC
        msg data.

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

    @property
    def sender(self) -> tuple[str, str]|None:
        if (
            (msg := self._ipc_msg)
            and (value := msg.sender)
        ):
            return tuple(value)


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

    Normally the source of an error is re-raised from some
    `.msg._codec` decode which itself raises in a backend interchange
    lib (eg. a `msgspec.ValidationError`).

    '''
    reprol_fields: list[str] = [
        'expected_msg_type',
    ]
    extra_body_fields: list[str] = [
        'cid',
        'expected_msg',
    ]

    @property
    def bad_msg(self) -> PayloadMsg|None:
        '''
        Ref to the the original invalid IPC shuttle msg which failed
        to decode thus providing for the reason for this error.

        '''
        if (
            (_bad_msg := self.msgdata.get('_bad_msg'))
            and
            isinstance(_bad_msg, PayloadMsg)
        ):
            return _bad_msg

        elif bad_msg_dict := self.bad_msg_as_dict:
            return msgtypes.from_dict_msg(
                dict_msg=bad_msg_dict.copy(),
                # use_pretty=True,
                # ^-TODO-^ would luv to use this BUT then the
                # `field_prefix` in `pformat_boxed_tb()` cucks it
                # all up.. XD
            )

        return None

    @property
    def bad_msg_as_dict(self) -> dict[str, Any]:
        '''
        If the underlying IPC `MsgType` was received from a remote
        actor but was unable to be decoded to a native `PayloadMsg`
        (`Yield`|`Started`|`Return`) struct, the interchange backend
        native format decoder can be used to stash a `dict` version
        for introspection by the invalidating RPC task.

        Optionally when this error is constructed from
        `.from_decode()` the caller can attempt to construct what
        would have been the original `MsgType`-with-payload subtype
        (i.e. an instance from the set of msgs in
        `.msg.types._payload_msgs`) which failed validation.

        '''
        return self.msgdata.get('_bad_msg_as_dict')

    @property
    def expected_msg_type(self) -> Type[MsgType]|None:
        return type(self.bad_msg)

    @property
    def cid(self) -> str:
        # pull from required `.bad_msg` ref (or src dict)
        if bad_msg := self.bad_msg:
            return bad_msg.cid

        return self.msgdata['cid']

    @classmethod
    def from_decode(
        cls,
        message: str,

        bad_msg: PayloadMsg|None = None,
        bad_msg_as_dict: dict|None = None,

        # if provided, expand and pack all RAE compat fields into the
        # `._extra_msgdata` auxillary data `dict` internal to
        # `RemoteActorError`.
        **extra_msgdata,

    ) -> MsgTypeError:
        '''
        Constuctor for easy creation from (presumably) catching
        the backend interchange lib's underlying validation error
        and passing context-specific meta-data to `_mk_msg_type_err()`
        (which is normally the caller of this).

        '''
        if bad_msg_as_dict:
            # NOTE: original "vanilla decode" of the msg-bytes
            # is placed inside a value readable from
            # `.msgdata['_msg_dict']`
            extra_msgdata['_bad_msg_as_dict'] = bad_msg_as_dict

            # scrape out any underlying fields from the
            # msg that failed validation.
            for k, v in bad_msg_as_dict.items():
                if (
                    # always skip a duplicate entry
                    # if already provided as an arg
                    k == '_bad_msg' and bad_msg
                    or
                    # skip anything not in the default msg-field set.
                    k not in _ipcmsg_keys
                    # k not in _body_fields
                ):
                    continue

                extra_msgdata[k] = v


        elif bad_msg:
            if not isinstance(bad_msg, PayloadMsg):
                raise TypeError(
                    'The provided `bad_msg` is not a `PayloadMsg` type?\n\n'
                    f'{bad_msg}'
                )
            extra_msgdata['_bad_msg'] = bad_msg
            extra_msgdata['cid'] = bad_msg.cid

        extra_msgdata.setdefault('boxed_type', cls)
        return cls(
            message=message,
            **extra_msgdata,
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


class TransportClosed(trio.BrokenResourceError):
    '''
    IPC transport (protocol) connection was closed or broke and
    indicates that the wrapping communication `Channel` can no longer
    be used to send/receive msgs from the remote peer.

    '''
    def __init__(
        self,
        message: str,
        loglevel: str = 'transport',
        cause: BaseException|None = None,
        raise_on_report: bool = False,

    ) -> None:
        self.message: str = message
        self._loglevel = loglevel
        super().__init__(message)

        if cause is not None:
            self.__cause__ = cause

        # flag to toggle whether the msg loop should raise
        # the exc in its `TransportClosed` handler block.
        self._raise_on_report = raise_on_report

    def report_n_maybe_raise(
        self,
        message: str|None = None,

    ) -> None:
        '''
        Using the init-specified log level emit a logging report
        for this error.

        '''
        message: str = message or self.message
        # when a cause is set, slap it onto the log emission.
        if cause := self.__cause__:
            cause_tb_str: str = ''.join(
                traceback.format_tb(cause.__traceback__)
            )
            message += (
                f'{cause_tb_str}\n'  # tb
                f'    {cause}\n'  # exc repr
            )

        getattr(log, self._loglevel)(message)

        # some errors we want to blow up from
        # inside the RPC msg loop
        if self._raise_on_report:
            raise self from cause


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

    cid: str|None = None,
    src_uid: tuple[str, str]|None = None,
    tb: TracebackType|None = None,
    tb_str: str = '',
    message: str = '',

) -> Error:
    '''
    Create an "error message" which boxes a locally caught
    exception's meta-data and encodes it for wire transport via an
    IPC `Channel`; expected to be unpacked (and thus unboxed) on
    the receiver side using `unpack_error()` below.

    '''
    if not tb_str:
        tb_str: str = (
            ''.join(traceback.format_exception(exc))

            # TODO: can we remove this since `exc` is required.. right?
            or
            # NOTE: this is just a shorthand for the "last error" as
            # provided by `sys.exeception()`, see:
            # - https://docs.python.org/3/library/traceback.html#traceback.print_exc
            # - https://docs.python.org/3/library/traceback.html#traceback.format_exc
            traceback.format_exc()
        )
    else:
        if tb_str[-2:] != '\n':
            tb_str += '\n'

    # when caller provides a tb instance (say pulled from some other
    # src error's `.__traceback__`) we use that as the "boxed"
    # tb-string instead.
    # https://docs.python.org/3/library/traceback.html#traceback.format_list
    if tb:
        tb_str: str = ''.join(traceback.format_tb(tb)) + tb_str

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

    # XXX always append us the last relay in error propagation path
    error_msg.setdefault(
        'relay_path',
        [],
    ).append(our_uid)

    # XXX NOTE XXX always ensure the traceback-str content is from
    # the locally raised error (so, NOT the prior relay's boxed
    # `._ipc_msg.tb_str`).
    error_msg['tb_str'] = tb_str
    error_msg['message'] = message or getattr(exc, 'message', '')
    if cid is not None:
        error_msg['cid'] = cid

    return Error(**error_msg)


def unpack_error(
    msg: Error,
    chan: Channel,
    box_type: RemoteActorError = RemoteActorError,

) -> None|Exception:
    '''
    Unpack an 'error' message from the wire
    into a local `RemoteActorError` (subtype).

    NOTE: this routine DOES not RAISE the embedded remote error,
    which is the responsibilitiy of the caller.

    '''
    if not isinstance(msg, Error):
        return None

    # try to lookup a suitable error type from the local runtime
    # env then use it to construct a local instance.
    # boxed_type_str: str = error_dict['boxed_type_str']
    boxed_type_str: str = msg.boxed_type_str
    boxed_type: Type[BaseException] = get_err_type(boxed_type_str)

    # retrieve the error's msg-encoded remotoe-env info
    message: str = f'remote task raised a {msg.boxed_type_str!r}\n'

    # TODO: do we even really need these checks for RAEs?
    if boxed_type_str in [
        'ContextCancelled',
        'MsgTypeError',
    ]:
        box_type = {
            'ContextCancelled': ContextCancelled,
            'MsgTypeError': MsgTypeError,
        }[boxed_type_str]
        assert boxed_type is box_type

    # TODO: already included by `_this_mod` in else loop right?
    #
    # we have an inception/onion-error so ensure
    # we include the relay_path info and the
    # original source error.
    elif boxed_type_str == 'RemoteActorError':
        assert boxed_type is RemoteActorError
        assert len(msg.relay_path) >= 1

    exc = box_type(
        message,
        ipc_msg=msg,
        tb_str=msg.tb_str,
    )

    return exc


def is_multi_cancelled(
    exc: BaseException|BaseExceptionGroup
) -> bool:
    '''
    Predicate to determine if a possible ``BaseExceptionGroup`` contains
    only ``trio.Cancelled`` sub-exceptions (and is likely the result of
    cancelling a collection of subtasks.

    '''
    if isinstance(exc, BaseExceptionGroup):
        return exc.subgroup(
            lambda exc: isinstance(exc, trio.Cancelled)
        ) is not None

    return False


def _raise_from_unexpected_msg(
    ctx: Context,
    msg: MsgType,
    src_err: Exception,
    log: StackLevelAdapter,  # caller specific `log` obj

    expect_msg: Type[MsgType],

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
    except AttributeError as src_err:
        raise MessagingError(
            f'IPC `Context` rx-ed msg without a ctx-id (cid)!?\n'
            f'cid: {cid}\n\n'

            f'{pformat(msg)}\n'
        ) from src_err

    # TODO: test that shows stream raising an expected error!!!
    stream: MsgStream|None
    _type: str = 'Context'

    # raise the error message in a boxed exception type!
    if isinstance(msg, Error):
    # match msg:
    #     case Error():
        exc: RemoteActorError = unpack_error(
            msg,
            ctx.chan,
        )
        ctx._maybe_cancel_and_set_remote_error(exc)
        raise exc from src_err

    # `MsgStream` termination msg.
    # TODO: does it make more sense to pack 
    # the stream._eoc outside this in the calleer always?
        # case Stop():
    elif stream := ctx._stream:
        _type: str = 'MsgStream'

        if (
            stream._eoc
            or
            isinstance(msg, Stop)
        ):
            message: str = (
                f'Context[{cid}] stream was stopped by remote side\n'
                f'cid: {cid}\n'
            )
            log.debug(message)

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
            eoc.add_note(message)

            # XXX: important to set so that a new `.receive()`
            # call (likely by another task using a broadcast receiver)
            # doesn't accidentally pull the `return` message
            # value out of the underlying feed mem chan which is
            # destined for the `Context.result()` call during ctx-exit!
            stream._eoc: Exception = eoc

            # in case there already is some underlying remote error
            # that arrived which is probably the source of this stream
            # closure
            ctx.maybe_raise(from_src_exc=src_err)
            raise eoc from src_err

        # TODO: our own transport/IPC-broke error subtype?
        if stream._closed:
            raise trio.ClosedResourceError('This stream was closed')

    # always re-raise the source error if no translation error case
    # is activated above.
    raise MessagingError(
        f'{_type} was expecting a {expect_msg.__name__!r} message'
        ' BUT received a non-error msg:\n\n'
        f'{struct_format(msg)}'
    ) from src_err
    # ^-TODO-^ maybe `MsgDialogError` is better?


_raise_from_no_key_in_msg = _raise_from_unexpected_msg


def _mk_send_mte(
    msg: Any|bytes|MsgType,
    codec: MsgCodec|MsgDec,

    message: str|None = None,
    verb_header: str = '',

    src_type_error: TypeError|None = None,
    is_invalid_payload: bool = False,

    **mte_kwargs,

) -> MsgTypeError:
    '''
    Compose a `MsgTypeError` from a `Channel.send()`-side error,
    normally raised witih a runtime IPC `Context`.

    '''
    if isinstance(codec, MsgDec):
        raise RuntimeError(
            '`codec` must be a `MsgCodec` for send-side errors?'
        )

    from tractor.devx import (
        pformat_caller_frame,
    )
    # no src error from `msgspec.msgpack.Decoder.decode()` so
    # prolly a manual type-check on our part.
    if message is None:
        tb_fmt: str = pformat_caller_frame(stack_limit=3)
        message: str = (
            f'invalid msg -> {msg}: {type(msg)}\n\n'
            f'{tb_fmt}\n'
            f'Valid IPC msgs are:\n\n'
            f'{codec.msg_spec_str}\n',
        )
    elif src_type_error:
        src_message: str = str(src_type_error)
        patt: str = 'type '
        type_idx: int = src_message.find('type ')
        invalid_type: str = src_message[type_idx + len(patt):].split()[0]

        enc_hook: Callable|None = codec.enc.enc_hook
        if enc_hook is None:
            message += (
                '\n\n'

                f"The current IPC-msg codec can't encode type `{invalid_type}` !\n"
                f'Maybe a `msgpack.Encoder.enc_hook()` extension is needed?\n\n'

                f'Check the `msgspec` docs for ad-hoc type extending:\n'
                '|_ https://jcristharif.com/msgspec/extending.html\n'
                '|_ https://jcristharif.com/msgspec/extending.html#defining-a-custom-extension-messagepack-only\n'
            )

    msgtyperr = MsgTypeError(
        message=message,
        _bad_msg=msg,
    )
    # ya, might be `None`
    msgtyperr.__cause__ = src_type_error
    return msgtyperr


def _mk_recv_mte(
    msg: Any|bytes|MsgType,
    codec: MsgCodec|MsgDec,

    message: str|None = None,
    verb_header: str = '',

    src_validation_error: ValidationError|None = None,
    is_invalid_payload: bool = False,

    **mte_kwargs,

) -> MsgTypeError:
    '''
    Compose a `MsgTypeError` from a
    `Channel|Context|MsgStream.receive()`-side error,
    normally raised witih a runtime IPC ctx or streaming
    block.

    '''
    msg_dict: dict|None = None
    bad_msg: PayloadMsg|None = None

    if is_invalid_payload:
        msg_type: str = type(msg)
        any_pld: Any = msgpack.decode(msg.pld)
        message: str = (
            f'invalid `{msg_type.__qualname__}` msg payload\n\n'
            f'value: `{any_pld!r}` does not match type-spec: '
            f'`{type(msg).__qualname__}.pld: {codec.pld_spec_str}`'
        )
        bad_msg = msg

    else:
        # decode the msg-bytes using the std msgpack
        # interchange-prot (i.e. without any `msgspec.Struct`
        # handling) so that we can determine what
        # `.msg.types.PayloadMsg` is the culprit by reporting the
        # received value.
        msg: bytes
        msg_dict: dict = msgpack.decode(msg)
        msg_type_name: str = msg_dict['msg_type']
        msg_type = getattr(msgtypes, msg_type_name)
        message: str = (
            f'invalid `{msg_type_name}` IPC msg\n\n'
        )
        # XXX be "fancy" and see if we can determine the exact
        # invalid field such that we can comprehensively report
        # the specific field's type problem.
        msgspec_msg: str = src_validation_error.args[0].rstrip('`')
        msg, _, maybe_field = msgspec_msg.rpartition('$.')
        obj = object()
        if (field_val := msg_dict.get(maybe_field, obj)) is not obj:
            field_name_expr: str = (
                f' |_{maybe_field}: {codec.pld_spec_str} = '
            )
            fmt_val_lines: list[str] = pformat(field_val).splitlines()
            fmt_val: str = (
                f'{fmt_val_lines[0]}\n'
                +
                textwrap.indent(
                    '\n'.join(fmt_val_lines[1:]),
                    prefix=' '*len(field_name_expr),
                )
            )
            message += (
                f'{msg.rstrip("`")}\n\n'
                f'<{msg_type.__qualname__}(\n'
                # f'{".".join([msg_type.__module__, msg_type.__qualname__])}\n'
                f'{field_name_expr}{fmt_val}\n'
                f')>'
            )

    if verb_header:
        message = f'{verb_header} ' + message

    msgtyperr = MsgTypeError.from_decode(
        message=message,
        bad_msg=bad_msg,
        bad_msg_as_dict=msg_dict,
        boxed_type=type(src_validation_error),

        # NOTE: for pld-spec MTEs we set the `._ipc_msg` manually:
        # - for the send-side `.started()` pld-validate
        #   case we actually raise inline so we don't need to
        #   set the it at all.
        # - for recv side we set it inside `PldRx.decode_pld()`
        #   after a manual call to `pack_error()` since we
        #   actually want to emulate the `Error` from the mte we
        #   build here. So by default in that case, this is left
        #   as `None` here.
        #   ipc_msg=src_err_msg,
    )
    msgtyperr.__cause__ = src_validation_error
    return msgtyperr
