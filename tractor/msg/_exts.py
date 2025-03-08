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
Type-extension-utils for codec-ing (python) objects not
covered by the `msgspec.msgpack` protocol.

See the various API docs from `msgspec`.

extending from native types,
- https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types

converters,
- https://jcristharif.com/msgspec/converters.html
- https://jcristharif.com/msgspec/api.html#msgspec.convert

`Raw` fields,
- https://jcristharif.com/msgspec/api.html#raw
- support for `.convert()` and `Raw`,
  |_ https://jcristharif.com/msgspec/changelog.html

'''
from types import (
    ModuleType,
)
import typing
from typing import (
    Type,
    Union,
)

def dec_type_union(
    type_names: list[str],
    mods: list[ModuleType] = []
) -> Type|Union[Type]:
    '''
    Look up types by name, compile into a list and then create and
    return a `typing.Union` from the full set.

    '''
    # import importlib
    types: list[Type] = []
    for type_name in type_names:
        for mod in [
            typing,
            # importlib.import_module(__name__),
        ] + mods:
            if type_ref := getattr(
                mod,
                type_name,
                False,
            ):
                types.append(type_ref)

    # special case handling only..
    # ipc_pld_spec: Union[Type] = eval(
    #     pld_spec_str,
    #     {},  # globals
    #     {'typing': typing},  # locals
    # )

    return Union[*types]


def enc_type_union(
    union_or_type: Union[Type]|Type,
) -> list[str]:
    '''
    Encode a type-union or single type to a list of type-name-strings
    ready for IPC interchange.

    '''
    type_strs: list[str] = []
    for typ in getattr(
        union_or_type,
        '__args__',
        {union_or_type,},
    ):
        type_strs.append(typ.__qualname__)

    return type_strs
