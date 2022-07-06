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
Capability-based messaging specifications: or colloquially as "msgspecs".

Includes our SCIPP (structured-con-inter-process-protocol) message type defs
and APIs for applying custom msgspec-sets for implementing un-protocol state machines.

'''

# TODO: integration with our ``enable_modules: list[str]`` caps sys.

# ``pkgutil.resolve_name()`` internally uses
# ``importlib.import_module()`` which can be filtered by inserting
# a ``MetaPathFinder`` into ``sys.meta_path`` (which we could do before
# entering the ``Actor._process_messages()`` loop).
# https://github.com/python/cpython/blob/main/Lib/pkgutil.py#L645
# https://stackoverflow.com/questions/1350466/preventing-python-code-from-importing-certain-modules
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
from typing import (
    Union,
    Any,
)

from msgspec import Struct
from msgspec.msgpack import (
    Encoder,
    Decoder,
)


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
    global _lifo_codecs

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
