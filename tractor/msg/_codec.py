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
IPC msg interchange codec management.

Supported backend libs:
- `msgspec.msgpack`

ToDo: backends we prolly should offer:

- see project/lib list throughout GH issue discussion comments:
  https://github.com/goodboy/tractor/issues/196

- `capnproto`: https://capnproto.org/rpc.html
   - https://capnproto.org/language.html#language-reference

'''
from contextvars import (
    ContextVar,
    Token,
)
from contextlib import (
    contextmanager as cm,
)
from typing import (
    Any,
    Callable,
    Type,
    Union,
)
from types import ModuleType

import msgspec
from msgspec import msgpack

from .pretty_struct import Struct


# TODO: API changes towards being interchange lib agnostic!
# -[ ] capnproto has pre-compiled schema for eg..
#  * https://capnproto.org/language.html
#  * http://capnproto.github.io/pycapnp/quickstart.html
#   * https://github.com/capnproto/pycapnp/blob/master/examples/addressbook.capnp
class MsgCodec(Struct):
    '''
    A IPC msg interchange format lib's encoder + decoder pair.

    '''

    lib: ModuleType = msgspec

    # ad-hoc type extensions
    # https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types
    enc_hook: Callable[[Any], Any]|None = None  # coder
    dec_hook: Callable[[type, Any], Any]|None = None # decoder

    # struct type unions
    # https://jcristharif.com/msgspec/structs.html#tagged-unions
    types: Union[Type[Struct]]|Any = Any

    # post-configure cached props
    _enc: msgpack.Encoder|None = None
    _dec: msgpack.Decoder|None = None


    # TODO: use `functools.cached_property` for these ?
    # https://docs.python.org/3/library/functools.html#functools.cached_property
    @property
    def enc(self) -> msgpack.Encoder:
        return self._enc or self.encoder()

    def encoder(
        self,
        enc_hook: Callable|None = None,
        reset: bool = False,

        # TODO: what's the default for this?
        # write_buffer_size: int
        **kwargs,

    ) -> msgpack.Encoder:
        '''
        Set or get the maybe-cached `msgspec.msgpack.Encoder`
        instance configured for this codec.

        When `reset=True` any previously configured encoder will
        be recreated and then cached with the new settings passed
        as input.

        '''
        if (
            self._enc is None
            or reset
        ):
            self._enc = self.lib.msgpack.Encoder(
                enc_hook=enc_hook or self.enc_hook,
                # write_buffer_size=write_buffer_size,
            )

        return self._enc

    def encode(
        self,
        py_obj: Any,

    ) -> bytes:
        '''
        Encode input python objects to `msgpack` bytes for transfer
        on a tranport protocol connection.

        '''
        return self.enc.encode(py_obj)

    @property
    def dec(self) -> msgpack.Decoder:
        return self._dec or self.decoder()

    def decoder(
        self,
        types: Union[Type[Struct]]|None = None,
        dec_hook: Callable|None = None,
        reset: bool = False,
        **kwargs,
        # ext_hook: ext_hook_sig

    ) -> msgpack.Decoder:
        '''
        Set or get the maybe-cached `msgspec.msgpack.Decoder`
        instance configured for this codec.

        When `reset=True` any previously configured decoder will
        be recreated and then cached with the new settings passed
        as input.

        '''
        if (
            self._dec is None
            or reset
        ):
            self._dec = self.lib.msgpack.Decoder(
                types or self.types,
                dec_hook=dec_hook or self.dec_hook,
                **kwargs,
            )

        return self._dec

    def decode(
        self,
        msg: bytes,
    ) -> Any:
        '''
        Decode received `msgpack` bytes into a local python object
        with special `msgspec.Struct` (or other type) handling
        determined by the 

        '''

        return self.dec.decode(msg)


# TODO: struct aware messaging coders as per:
# - https://github.com/goodboy/tractor/issues/36
# - https://github.com/goodboy/tractor/issues/196
# - https://github.com/goodboy/tractor/issues/365

def mk_codec(
    libname: str = 'msgspec',

    # struct type unions set for `Decoder`
    # https://jcristharif.com/msgspec/structs.html#tagged-unions
    dec_types: Union[Type[Struct]]|Any = Any,

    cache_now: bool = True,

    # proxy to the `Struct.__init__()`
    **kwargs,

) -> MsgCodec:
    '''
    Convenience factory for creating codecs eventually meant
    to be interchange lib agnostic (i.e. once we support more then just
    `msgspec` ;).

    '''
    codec = MsgCodec(
        types=dec_types,
        **kwargs,
    )
    assert codec.lib.__name__ == libname

    # by default config and cache the codec pair for given
    # input settings.
    if cache_now:
        assert codec.enc
        assert codec.dec

    return codec


# instance of the default `msgspec.msgpack` codec settings, i.e.
# no custom structs, hooks or other special types.
_def_msgspec_codec: MsgCodec = mk_codec()

# NOTE: provides for per-`trio.Task` specificity of the
# IPC msging codec used by the transport layer when doing
# `Channel.send()/.recv()` of wire data.
_ctxvar_MsgCodec: ContextVar[MsgCodec] = ContextVar(
    'msgspec_codec',
    default=_def_msgspec_codec,
)


@cm
def apply_codec(
    codec: MsgCodec,

) -> MsgCodec:
    '''
    Dynamically apply a `MsgCodec` to the current task's
    runtime context such that all IPC msgs are processed
    with it for that task.

    '''
    token: Token = _ctxvar_MsgCodec.set(codec)
    try:
        yield _ctxvar_MsgCodec.get()
    finally:
        _ctxvar_MsgCodec.reset(token)


def current_msgspec_codec() -> MsgCodec:
    '''
    Return the current `trio.Task.context`'s value
    for `msgspec_codec` used by `Channel.send/.recv()`
    for wire serialization.

    '''
    return _ctxvar_MsgCodec.get()
