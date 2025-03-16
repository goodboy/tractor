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
typing.Protocol based generic msg API, implement this class to add backends for
tractor.ipc.Channel

'''
import trio
from typing import (
    runtime_checkable,
    Protocol,
    TypeVar,
)
from collections.abc import AsyncIterator


# from tractor.msg.types import MsgType
# ?TODO? this should be our `Union[*msgtypes.__spec__]` alias now right..?
# => BLEH, except can't bc prots must inherit typevar or param-spec
#   vars..
MsgType = TypeVar('MsgType')


@runtime_checkable
class MsgTransport(Protocol[MsgType]):
#
# ^-TODO-^ consider using a generic def and indexing with our
# eventual msg definition/types?
# - https://docs.python.org/3/library/typing.html#typing.Protocol

    stream: trio.SocketStream
    drained: list[MsgType]

    def __init__(self, stream: trio.SocketStream) -> None:
        ...

    # XXX: should this instead be called `.sendall()`?
    async def send(self, msg: MsgType) -> None:
        ...

    async def recv(self) -> MsgType:
        ...

    def __aiter__(self) -> MsgType:
        ...

    def connected(self) -> bool:
        ...

    # defining this sync otherwise it causes a mypy error because it
    # can't figure out it's a generator i guess?..?
    def drain(self) -> AsyncIterator[dict]:
        ...

    @property
    def laddr(self) -> tuple[str, int]:
        ...

    @property
    def raddr(self) -> tuple[str, int]:
        ...
