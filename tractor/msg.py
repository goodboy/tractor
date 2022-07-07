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

# TODO: integration with our ``enable_modules: list[str]`` caps sys.

# ``pkgutil.resolve_name()`` internally uses
# ``importlib.import_module()`` which can be filtered by inserting
# a ``MetaPathFinder`` into ``sys.meta_path`` (which we could do before
# entering the ``_runtime.process_messages()`` loop).
# - https://github.com/python/cpython/blob/main/Lib/pkgutil.py#L645
# - https://stackoverflow.com/questions/1350466/preventing-python-code-from-importing-certain-modules
#   - https://stackoverflow.com/a/63320902
#   - https://docs.python.org/3/library/sys.html#sys.meta_path

# the new "Implicit Namespace Packages" might be relevant?
# - https://www.python.org/dev/peps/pep-0420/

# add implicit serialized message type support so that paths can be
# handed directly to IPC primitives such as streams and `Portal.run()`
# calls:
# - via ``msgspec``:
#   - https://jcristharif.com/msgspec/api.html#struct
#   - https://jcristharif.com/msgspec/extending.html
# via ``msgpack-python``:
# https://github.com/msgpack/msgpack-python#packingunpacking-of-custom-data-type

from __future__ import annotations
from contextlib import contextmanager as cm
from pkgutil import resolve_name
from typing import Union, Any, Optional


from msgspec import Struct, Raw
from msgspec.msgpack import (
    Encoder,
    Decoder,
)


class NamespacePath(str):
    '''
    A serializeable description of a (function) Python object location
    described by the target's module path and namespace key meant as
    a message-native "packet" to allows actors to point-and-load objects
    by absolute reference.

    '''
    _ref: object = None

    def load_ref(self) -> object:
        if self._ref is None:
            self._ref = resolve_name(self)
        return self._ref

    def to_tuple(
        self,

    ) -> tuple[str, str]:
        ref = self.load_ref()
        return ref.__module__, getattr(ref, '__name__', '')

    @classmethod
    def from_ref(
        cls,
        ref,

    ) -> NamespacePath:
        return cls(':'.join(
            (ref.__module__,
             getattr(ref, '__name__', ''))
        ))


# LIFO codec stack that is appended when the user opens the
# ``configure_native_msgs()`` cm below to configure a new codec set
# which will be applied to all new (msgspec relevant) IPC transports
# that are spawned **after** the configure call is made.
_lifo_codecs: list[
    tuple[
        Encoder,
        Decoder,
    ],
] = [(Encoder(), Decoder())]


def get_msg_codecs() -> tuple[
    Encoder,
    Decoder,
]:
    '''
    Return the currently configured ``msgspec`` codec set.

    The defaults are defined above.

    '''
    global _lifo_codecs
    return _lifo_codecs[-1]


@cm
def configure_native_msgs(
    tagged_structs: list[Struct],
):
    '''
    Push a codec set that will natively decode
    tagged structs provied in ``tagged_structs``
    in all IPC transports and pop the codec on exit.

    '''
    # See "tagged unions" docs:
    # https://jcristharif.com/msgspec/structs.html#tagged-unions

    # "The quickest way to enable tagged unions is to set tag=True when
    # defining every struct type in the union. In this case tag_field
    # defaults to "type", and tag defaults to the struct class name
    # (e.g. "Get")."
    enc = Encoder()

    types_union = Union[tagged_structs[0]] | Any
    for struct in tagged_structs[1:]:
        types_union |= struct

    dec = Decoder(types_union)

    _lifo_codecs.append((enc, dec))
    try:
        print("YOYOYOOYOYOYOY")
        yield enc, dec
    finally:
        print("NONONONONON")
        _lifo_codecs.pop()


class Header(Struct, tag=True):
    '''
    A msg header which defines payload properties

    '''
    uid: str
    msgtype: Optional[str] = None


class Msg(Struct, tag=True):
    '''
    The "god" msg type, a box for task level msg types.

    '''
    header: Header
    payload: Raw


_root_dec = Decoder(Msg)
_root_enc = Encoder()

# sub-decoders for retreiving embedded
# payload data and decoding to a sender
# side defined (struct) type.
_subdecs:  dict[
    Optional[str],
    Decoder] = {
    None: Decoder(Any),
}


@cm
def enable_context(
    msg_subtypes: list[list[Struct]]
) -> Decoder:

    for types in msg_subtypes:
        first = types[0]

        # register using the default tag_field of "type"
        # which seems to map to the class "name".
        tags = [first.__name__]

        # create a tagged union decoder for this type set
        type_union = Union[first]
        for typ in types[1:]:
            type_union |= typ
            tags.append(typ.__name__)

        dec = Decoder(type_union)

        # register all tags for this union sub-decoder
        for tag in tags:
            _subdecs[tag] = dec
        try:
            yield dec
        finally:
            for tag in tags:
                _subdecs.pop(tag)


def decmsg(msg: Msg) -> Any:
    msg = _root_dec.decode(msg)
    tag_field = msg.header.msgtype
    dec = _subdecs[tag_field]
    return dec.decode(msg.payload)


def encmsg(
    dialog_id: str | int,
    payload: Any,
) -> Msg:

    tag_field = None

    plbytes = _root_enc.encode(payload)
    if b'type' in plbytes:
        assert isinstance(payload, Struct)
        tag_field = type(payload).__name__
        payload = Raw(plbytes)

    msg = Msg(
        Header(dialog_id, tag_field),
        payload,
    )
    return _root_enc.encode(msg)
