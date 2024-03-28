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

from tractor.msg.pretty_struct import Struct
from tractor.msg.types import (
    mk_msg_spec,
    Msg,
)


# TODO: API changes towards being interchange lib agnostic!
#
# -[ ] capnproto has pre-compiled schema for eg..
#  * https://capnproto.org/language.html
#  * http://capnproto.github.io/pycapnp/quickstart.html
#   * https://github.com/capnproto/pycapnp/blob/master/examples/addressbook.capnp
#
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
    ipc_msg_spec: Union[Type[Struct]]|Any = Any
    payload_msg_spec: Union[Type[Struct]] = Any

    # post-configure cached props
    _enc: msgpack.Encoder|None = None
    _dec: msgpack.Decoder|None = None

    # TODO: a sub-decoder system as well?
    # see related comments in `.msg.types`
    # _payload_decs: (
    #     dict[
    #         str,
    #         msgpack.Decoder,
    #     ]
    #     |None
    # ) = None

    # TODO: use `functools.cached_property` for these ?
    # https://docs.python.org/3/library/functools.html#functools.cached_property
    @property
    def enc(self) -> msgpack.Encoder:
        return self._enc or self.encoder()

    def encoder(
        self,
        enc_hook: Callable|None = None,
        reset: bool = False,

        # TODO: what's the default for this, and do we care?
        # write_buffer_size: int
        #
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
        ipc_msg_spec: Union[Type[Struct]]|None = None,
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
                type=ipc_msg_spec or self.ipc_msg_spec,
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


def mk_tagged_union_dec(
    tagged_structs: list[Struct],

) -> tuple[
    list[str],
    msgpack.Decoder,
]:
    # See "tagged unions" docs:
    # https://jcristharif.com/msgspec/structs.html#tagged-unions

    # "The quickest way to enable tagged unions is to set tag=True when
    # defining every struct type in the union. In this case tag_field
    # defaults to "type", and tag defaults to the struct class name
    # (e.g. "Get")."
    first: Struct = tagged_structs[0]
    types_union: Union[Type[Struct]] = Union[
       first
    ]|Any
    tags: list[str] = [first.__name__]

    for struct in tagged_structs[1:]:
        types_union |= struct
        tags.append(struct.__name__)

    dec = msgpack.Decoder(types_union)
    return (
        tags,
        dec,
    )

# TODO: struct aware messaging coders as per:
# - https://github.com/goodboy/tractor/issues/36
# - https://github.com/goodboy/tractor/issues/196
# - https://github.com/goodboy/tractor/issues/365

def mk_codec(
    libname: str = 'msgspec',

    # for codec-ing boxed `Msg`-with-payload msgs
    payload_types: Union[Type[Struct]]|None = None,

    # TODO: do we want to allow NOT/using a diff `Msg`-set?
    #
    # struct type unions set for `Decoder`
    # https://jcristharif.com/msgspec/structs.html#tagged-unions
    ipc_msg_spec: Union[Type[Struct]]|Any = Any,

    cache_now: bool = True,

    # proxy as `Struct(**kwargs)`
    **kwargs,

) -> MsgCodec:
    '''
    Convenience factory for creating codecs eventually meant
    to be interchange lib agnostic (i.e. once we support more then just
    `msgspec` ;).

    '''
    # (manually) generate a msg-payload-spec for all relevant
    # god-boxing-msg subtypes, parameterizing the `Msg.pld: PayloadT`
    # for the decoder such that all sub-type msgs in our SCIPP
    # will automatically decode to a type-"limited" payload (`Struct`)
    # object (set).
    payload_type_spec: Union[Type[Msg]]|None = None
    if payload_types:
        (
            payload_type_spec,
            msg_types,
        ) = mk_msg_spec(
            payload_type=payload_types,
        )
        assert len(payload_type_spec.__args__) == len(msg_types)

        # TODO: sub-decode `.pld: Raw`?
        # see similar notes inside `.msg.types`..
        #
        # not sure we'll end up wanting/needing this
        # though it might have unforeseen advantages in terms
        # of enabling encrypted appliciation layer (only)
        # payloads?
        #
        # register sub-payload decoders to load `.pld: Raw`
        # decoded `Msg`-packets using a dynamic lookup (table)
        # instead of a pre-defined msg-spec via `Generic`
        # parameterization.
        #
        # (
        #     tags,
        #     payload_dec,
        # ) = mk_tagged_union_dec(
        #     tagged_structs=list(payload_types.__args__),
        # )
        # _payload_decs: (
        #     dict[str, msgpack.Decoder]|None
        # ) = {
        #     # pre-seed decoders for std-py-type-set for use when
        #     # `Msg.pld == None|Any`.
        #     None: msgpack.Decoder(Any),
        #     Any: msgpack.Decoder(Any),
        # }
        # for name in tags:
        #     _payload_decs[name] = payload_dec

    codec = MsgCodec(
        ipc_msg_spec=ipc_msg_spec,
        payload_msg_spec=payload_type_spec,
        **kwargs,
    )
    assert codec.lib.__name__ == libname

    # by default, config-n-cache the codec pair from input settings.
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


@cm
def limit_msg_spec(
    payload_types: Union[Type[Struct]],

    # TODO: don't need this approach right?
    #
    # tagged_structs: list[Struct]|None = None,

    **codec_kwargs,
):
    '''
    Apply a `MsgCodec` that will natively decode the SC-msg set's
    `Msg.pld: Union[Type[Struct]]` payload fields using
    tagged-unions of `msgspec.Struct`s from the `payload_types`
    for all IPC contexts in use by the current `trio.Task`.

    '''
    msgspec_codec: MsgCodec = mk_codec(
        payload_types=payload_types,
        **codec_kwargs,
    )
    with apply_codec(msgspec_codec):
        yield msgspec_codec
