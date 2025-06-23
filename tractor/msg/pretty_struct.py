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
Prettified version of `msgspec.Struct` for easier console grokin.

'''
from __future__ import annotations
from collections import UserList
import textwrap
from typing import (
    Any,
    Iterator,
)

from msgspec import (
    msgpack,
    Struct as _Struct,
    structs,
)
# from pprint import (
#     saferepr,
# )

from tractor.log import get_logger

log = get_logger()
# TODO: auto-gen type sig for input func both for
# type-msgs and logging of RPC tasks?
# taken and modified from:
# https://stackoverflow.com/a/57110117
# import inspect
# from typing import List

# def my_function(input_1: str, input_2: int) -> list[int]:
#     pass

# def types_of(func):
#     specs = inspect.getfullargspec(func)
#     return_type = specs.annotations['return']
#     input_types = [t.__name__ for s, t in specs.annotations.items() if s != 'return']
#     return f'{func.__name__}({": ".join(input_types)}) -> {return_type}'

# types_of(my_function)


class DiffDump(UserList):
    '''
    Very simple list delegator that repr() dumps (presumed) tuple
    elements of the form `tuple[str, Any, Any]` in a nice
    multi-line readable form for analyzing `Struct` diffs.

    '''
    def __repr__(self) -> str:
        if not len(self):
            return super().__repr__()

        # format by displaying item pair's ``repr()`` on multiple,
        # indented lines such that they are more easily visually
        # comparable when printed to console when printed to
        # console.
        repstr: str = '[\n'
        for k, left, right in self:
            repstr += (
                f'({k},\n'
                f' |_{repr(left)},\n'
                f' |_{repr(right)},\n'
                ')\n'
            )
        repstr += ']\n'
        return repstr


def iter_fields(struct: Struct) -> Iterator[
    tuple[
        structs.FieldIinfo,
        str,
        Any,
    ]
]:
    '''
    Iterate over all non-@property fields of this struct.

    '''
    fi: structs.FieldInfo
    for fi in structs.fields(struct):
        key: str = fi.name
        val: Any = getattr(struct, key)
        yield (
            fi,
            key,
            val,
        )


def iter_struct_ppfmt_lines(
    struct: Struct,
    field_indent: int = 0,
) -> Iterator[tuple[str, str]]:

    fi: structs.FieldInfo
    k: str
    v: Any
    for fi, k, v in iter_fields(struct):

        # TODO: how can we prefer `Literal['option1',  'option2,
        # ..]` over .__name__ == `Literal` but still get only the
        # latter for simple types like `str | int | None` etc..?
        ft: type = fi.type
        typ_name: str = getattr(
            ft,
            '__name__',
            str(ft)
        ).replace(' ', '')

        # recurse to get sub-struct's `.pformat()` output Bo
        if isinstance(v, Struct):
            yield from iter_struct_ppfmt_lines(
                struct=v,
                field_indent=field_indent+field_indent,
            )
        else:
            val_str: str = repr(v)

            # XXX LOL, below just seems to be f#$%in causing
            # recursion errs..
            #
            # the `pprint` recursion-safe format:
            # https://docs.python.org/3.11/library/pprint.html#pprint.saferepr
            # try:
            #     val_str: str = saferepr(v)
            # except Exception:
            #     log.exception(
            #         'Failed to `saferepr({type(struct)})` !?\n'
            #     )
                # raise
                # return _Struct.__repr__(struct)

        yield (
            ' '*field_indent,  # indented ws prefix
            f'{k}: {typ_name} = {val_str},',  # field's repr line content
        )


def pformat(
    struct: Struct,
    field_indent: int = 2,
    indent: int = 0,
) -> str:
    '''
    Recursion-safe `pprint.pformat()` style formatting of
    a `msgspec.Struct` for sane reading by a human using a REPL.

    '''
    obj_str: str = ''  # accumulator
    for prefix, field_repr, in iter_struct_ppfmt_lines(
        struct,
        field_indent=field_indent,
    ):
        obj_str += f'{prefix}{field_repr}\n'

    # global whitespace indent
    ws: str = ' '*indent
    if indent:
        obj_str: str = textwrap.indent(
            text=obj_str,
            prefix=ws,
        )

    # qtn: str = ws + struct.__class__.__qualname__
    qtn: str = struct.__class__.__qualname__

    return (
        f'{qtn}(\n'
        f'{obj_str}'
        f'{ws})'
    )


class Struct(
    _Struct,

    # https://jcristharif.com/msgspec/structs.html#tagged-unions
    # tag='pikerstruct',
    # tag=True,
):
    '''
    A "human friendlier" (aka repl buddy) struct subtype.

    '''
    def to_dict(
        self,
        include_non_members: bool = True,

    ) -> dict:
        '''
        Like it sounds.. direct delegation to:
        https://jcristharif.com/msgspec/api.html#msgspec.structs.asdict

        BUT, by default we pop all non-member (aka not defined as
        struct fields) fields by default.

        '''
        asdict: dict = structs.asdict(self)
        if include_non_members:
            return asdict

        # only return a dict of the struct members
        # which were provided as input, NOT anything
        # added as type-defined `@property` methods!
        sin_props: dict = {}
        fi: structs.FieldInfo
        for fi, k, v in iter_fields(self):
            sin_props[k] = asdict[k]

        return sin_props

    pformat = pformat

    def __repr__(self) -> str:
        try:
            return pformat(self)
        except Exception:
            log.exception(
                f'Failed to `pformat({type(self)})` !?\n'
            )
            return _Struct.__repr__(self)

    # __repr__ = pformat
    # __str__ = __repr__ = pformat
    # TODO: use a pprint.PrettyPrinter instance around ONLY rendering
    # inside a known tty?
    # def __repr__(self) -> str:
    #     ...

    def copy(
        self,
        update: dict | None = None,

    ) -> Struct:
        '''
        Validate-typecast all self defined fields, return a copy of
        us with all such fields.

        NOTE: This is kinda like the default behaviour in
        `pydantic.BaseModel` except a copy of the object is
        returned making it compat with `frozen=True`.

        '''
        if update:
            for k, v in update.items():
                setattr(self, k, v)

        # NOTE: roundtrip serialize to validate
        # - enode to msgpack binary format,
        # - decode that back to a struct.
        return msgpack.Decoder(type=type(self)).decode(
            msgpack.Encoder().encode(self)
        )

    def typecast(
        self,

        # TODO: allow only casting a named subset?
        # fields: set[str] | None = None,

    ) -> None:
        '''
        Cast all fields using their declared type annotations
        (kinda like what `pydantic` does by default).

        NOTE: this of course won't work on frozen types, use
        ``.copy()`` above in such cases.

        '''
        # https://jcristharif.com/msgspec/api.html#msgspec.structs.fields
        fi: structs.FieldInfo
        for fi in structs.fields(self):
            setattr(
                self,
                fi.name,
                fi.type(getattr(self, fi.name)),
            )

    # TODO: make a mod func instead and just point to it here for
    # method impl?
    def __sub__(
        self,
        other: Struct,

    ) -> DiffDump[tuple[str, Any, Any]]:
        '''
        Compare fields/items key-wise and return a `DiffDump`
        for easy visual REPL comparison B)

        '''
        diffs: DiffDump[tuple[str, Any, Any]] = DiffDump()
        for fi in structs.fields(self):
            attr_name: str = fi.name
            ours: Any = getattr(self, attr_name)
            theirs: Any = getattr(other, attr_name)
            if ours != theirs:
                diffs.append((
                    attr_name,
                    ours,
                    theirs,
                ))

        return diffs

    @classmethod
    def fields_diff(
        cls,
        other: dict|Struct,

    ) -> DiffDump[tuple[str, Any, Any]]:
        '''
        Very similar to `PrettyStruct.__sub__()` except accepts an
        input `other: dict` (presumably that would normally be called
        like `Struct(**other)`) which returns a `DiffDump` of the
        fields of the struct and the `dict`'s fields.

        '''
        nullish = object()
        consumed: dict = other.copy()
        diffs: DiffDump[tuple[str, Any, Any]] = DiffDump()
        for fi in structs.fields(cls):
            field_name: str = fi.name
            # ours: Any = getattr(self, field_name)
            theirs: Any = consumed.pop(field_name, nullish)
            if theirs is nullish:
                diffs.append((
                    field_name,
                    f'{fi.type!r}',
                    'NOT-DEFINED in `other: dict`',
                ))

        # when there are lingering fields in `other` that this struct
        # DOES NOT define we also append those.
        if consumed:
            for k, v in consumed.items():
                diffs.append((
                    k,
                    f'NOT-DEFINED for `{cls.__name__}`',
                    f'`other: dict` has value = {v!r}',
                ))

        return diffs
