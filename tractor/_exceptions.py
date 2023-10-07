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

"""
Our classy exception set.

"""
import builtins
import importlib
from typing import (
    Any,
    Type,
)
import traceback

import exceptiongroup as eg
import trio

from ._state import current_actor

_this_mod = importlib.import_module(__name__)


class ActorFailure(Exception):
    "General actor failure"


class RemoteActorError(Exception):
    '''
    Remote actor exception bundled locally

    '''
    # TODO: local recontruction of remote exception deats
    def __init__(
        self,
        message: str,
        suberror_type: Type[BaseException] | None = None,
        **msgdata

    ) -> None:
        super().__init__(message)

        self.type = suberror_type
        self.msgdata = msgdata

    @property
    def src_actor_uid(self) -> tuple[str, str] | None:
        return self.msgdata.get('src_actor_uid')


class InternalActorError(RemoteActorError):
    '''
    Remote internal ``tractor`` error indicating
    failure of some primitive or machinery.

    '''


class ContextCancelled(RemoteActorError):
    '''
    Inter-actor task context was cancelled by either a call to
    ``Portal.cancel_actor()`` or ``Context.cancel()``.

    '''
    @property
    def canceller(self) -> tuple[str, str] | None:
        value = self.msgdata.get('canceller')
        if value:
            return tuple(value)


class TransportClosed(trio.ClosedResourceError):
    "Underlying channel transport was closed prior to use"


class NoResult(RuntimeError):
    "No final result is expected for this actor"


class ModuleNotExposed(ModuleNotFoundError):
    "The requested module is not exposed for RPC"


class NoRuntime(RuntimeError):
    "The root actor has not been initialized yet"


class StreamOverrun(trio.TooSlowError):
    "This stream was overrun by sender"


class AsyncioCancelled(Exception):
    '''
    Asyncio cancelled translation (non-base) error
    for use with the ``to_asyncio`` module
    to be raised in the ``trio`` side task

    '''


def pack_error(
    exc: BaseException,
    tb=None,

) -> dict[str, Any]:
    """Create an "error message" for tranmission over
    a channel (aka the wire).
    """
    if tb:
        tb_str = ''.join(traceback.format_tb(tb))
    else:
        tb_str = traceback.format_exc()

    error_msg = {
        'tb_str': tb_str,
        'type_str': type(exc).__name__,
        'src_actor_uid': current_actor().uid,
    }

    if isinstance(exc, ContextCancelled):
        error_msg.update(exc.msgdata)

    return {'error': error_msg}


def unpack_error(

    msg: dict[str, Any],
    chan=None,
    err_type=RemoteActorError

) -> Exception:
    '''
    Unpack an 'error' message from the wire
    into a local ``RemoteActorError``.

    '''
    __tracebackhide__ = True
    error = msg['error']

    tb_str = error.get('tb_str', '')
    message = f'{chan.uid}\n' + tb_str
    type_name = error['type_str']
    suberror_type: Type[BaseException] = Exception

    if type_name == 'ContextCancelled':
        err_type = ContextCancelled
        suberror_type = err_type

    else:  # try to lookup a suitable local error type
        for ns in [
            builtins,
            _this_mod,
            eg,
            trio,
        ]:
            try:
                suberror_type = getattr(ns, type_name)
                break
            except AttributeError:
                continue

    exc = err_type(
        message,
        suberror_type=suberror_type,

        # unpack other fields into error type init
        **msg['error'],
    )

    return exc


def is_multi_cancelled(exc: BaseException) -> bool:
    '''
    Predicate to determine if a possible ``eg.BaseExceptionGroup`` contains
    only ``trio.Cancelled`` sub-exceptions (and is likely the result of
    cancelling a collection of subtasks.

    '''
    if isinstance(exc, eg.BaseExceptionGroup):
        return exc.subgroup(
            lambda exc: isinstance(exc, trio.Cancelled)
        ) is not None

    return False
