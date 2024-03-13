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

from tractor._state import current_actor
from tractor.log import get_logger

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
    'src_actor_uid',
    'canceller',
    'sender',
]

_msgdata_keys: list[str] = [
    'type_str',
] + _body_fields



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
        'src_actor_uid',
    ]

    def __init__(
        self,
        message: str,
        suberror_type: Type[BaseException] | None = None,
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
        self.boxed_type: str = suberror_type
        self.msgdata: dict[str, Any] = msgdata

    @property
    def type(self) -> str:
        return self.boxed_type

    @property
    def type_str(self) -> str:
        return str(type(self.boxed_type).__name__)

    @property
    def src_actor_uid(self) -> tuple[str, str]|None:
        return self.msgdata.get('src_actor_uid')

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

    def reprol(self) -> str:
        '''
        Represent this error for "one line" display, like in
        a field of our `Context.__repr__()` output.

        '''
        _repr: str = f'{type(self).__name__}('
        for key in self.reprol_fields:
            val: Any|None = self.msgdata.get(key)
            if val:
                _repr += f'{key}={repr(val)} '

        return _repr

    def __repr__(self) -> str:

        fields: str = ''
        for key in _body_fields:
            val: str|None = self.msgdata.get(key)
            if val:
                fields += f'{key}={val}\n'

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
            # f'|\n'
            # f'         |\n'
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

    # TODO: local recontruction of remote exception deats
    # def unbox(self) -> BaseException:
    #     ...


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

    # to make `.__repr__()` work uniformly
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
    'Some kind of unexpected SC messaging dialog issue'


def pack_error(
    exc: BaseException,
    tb: str|None = None,
    cid: str|None = None,

) -> dict[str, dict]:
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

    error_msg: dict[
        str,
        str | tuple[str, str]
    ] = {
        'tb_str': tb_str,
        'type_str': type(exc).__name__,
        'boxed_type': type(exc).__name__,
        'src_actor_uid': current_actor().uid,
    }

    # TODO: ?just wholesale proxy `.msgdata: dict`?
    # XXX WARNING, when i swapped these ctx-semantics
    # tests started hanging..???!!!???
    # if msgdata := exc.getattr('msgdata', {}):
    #     error_msg.update(msgdata)
    if (
        isinstance(exc, ContextCancelled)
        or isinstance(exc, StreamOverrun)
    ):
        error_msg.update(exc.msgdata)

    pkt: dict = {'error': error_msg}
    if cid:
        pkt['cid'] = cid

    return pkt


def unpack_error(

    msg: dict[str, Any],

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

    error_dict: dict[str, dict] | None
    if (
        error_dict := msg.get('error')
    ) is None:
        # no error field, nothing to unpack.
        return None

    # retrieve the remote error's msg encoded details
    tb_str: str = error_dict.get('tb_str', '')
    message: str = f'{chan.uid}\n' + tb_str
    type_name: str = (
        error_dict.get('type_str')
        or error_dict['boxed_type']
    )
    suberror_type: Type[BaseException] = Exception

    if type_name == 'ContextCancelled':
        box_type = ContextCancelled
        suberror_type = box_type

    else:  # try to lookup a suitable local error type
        for ns in [
            builtins,
            _this_mod,
            trio,
        ]:
            if suberror_type := getattr(
                ns,
                type_name,
                False,
            ):
                break

    exc = box_type(
        message,
        suberror_type=suberror_type,

        # unpack other fields into error type init
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
    msg: dict,
    src_err: KeyError,
    log: StackLevelAdapter,  # caller specific `log` obj

    expect_key: str = 'yield',
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
        cid: str = msg['cid']
    except KeyError as src_err:
        raise MessagingError(
            f'IPC `Context` rx-ed msg without a ctx-id (cid)!?\n'
            f'cid: {cid}\n\n'

            f'{pformat(msg)}\n'
        ) from src_err

    # TODO: test that shows stream raising an expected error!!!

    # raise the error message in a boxed exception type!
    if msg.get('error'):
        raise unpack_error(
            msg,
            ctx.chan,
            hide_tb=hide_tb,

        ) from None

    # `MsgStream` termination msg.
    # TODO: does it make more sense to pack 
    # the stream._eoc outside this in the calleer always?
    elif (
        msg.get('stop')
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

        raise eoc from src_err

    if (
        stream
        and stream._closed
    ):
        raise trio.ClosedResourceError('This stream was closed')


    # always re-raise the source error if no translation error case
    # is activated above.
    _type: str = 'Stream' if stream else 'Context'
    raise MessagingError(
        f"{_type} was expecting a '{expect_key}' message"
        " BUT received a non-error msg:\n"
        f'{pformat(msg)}'
    ) from src_err
