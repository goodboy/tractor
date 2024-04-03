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
Built-in messaging patterns, types, APIs and helpers.

'''
from typing import (
    Union,
    TypeAlias,
)
from .ptr import (
    NamespacePath as NamespacePath,
)
from .pretty_struct import (
    Struct as Struct,
)
from ._codec import (
    _def_msgspec_codec as _def_msgspec_codec,
    _ctxvar_MsgCodec as _ctxvar_MsgCodec,

    apply_codec as apply_codec,
    mk_codec as mk_codec,
    MsgCodec as MsgCodec,
    current_codec as current_codec,
)

from .types import (
    Msg as Msg,

    Aid as Aid,
    SpawnSpec as SpawnSpec,

    Start as Start,
    StartAck as StartAck,

    Started as Started,
    Yield as Yield,
    Stop as Stop,
    Return as Return,

    Error as Error,

    # full msg class set from above as list
    __msg_types__ as __msg_types__,
)
# TODO: use new type declaration syntax for msg-type-spec
# https://docs.python.org/3/library/typing.html#type-aliases
# https://docs.python.org/3/reference/simple_stmts.html#type
__msg_spec__: TypeAlias = Union[*__msg_types__]
