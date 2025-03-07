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
    mk_dec as mk_dec,
    MsgCodec as MsgCodec,
    MsgDec as MsgDec,
    current_codec as current_codec,
)
# currently can't bc circular with `._context`
# from ._ops import (
#     PldRx as PldRx,
#     _drain_to_final_msg as _drain_to_final_msg,
# )

from .types import (
    PayloadMsg as PayloadMsg,

    Aid as Aid,
    SpawnSpec as SpawnSpec,

    Start as Start,
    StartAck as StartAck,

    Started as Started,
    Yield as Yield,
    Stop as Stop,
    Return as Return,
    CancelAck as CancelAck,

    Error as Error,

    # type-var for `.pld` field
    PayloadT as PayloadT,

    # full msg class set from above as list
    __msg_types__ as __msg_types__,

    # type-alias for union of all msgs
    MsgType as MsgType,
)

__msg_spec__: TypeAlias = MsgType
