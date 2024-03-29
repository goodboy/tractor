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
from __future__ import annotations
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


# TODO: overall IPC msg-spec features (i.e. in this mod)!
#
# -[ ] API changes towards being interchange lib agnostic!
#   -[ ] capnproto has pre-compiled schema for eg..
#    * https://capnproto.org/language.html
#    * http://capnproto.github.io/pycapnp/quickstart.html
#     * https://github.com/capnproto/pycapnp/blob/master/examples/addressbook.capnp
#
# -[ ] struct aware messaging coders as per:
#   -[x] https://github.com/goodboy/tractor/issues/36
#   -[ ] https://github.com/goodboy/tractor/issues/196
#   -[ ] https://github.com/goodboy/tractor/issues/365
#
class MsgCodec(Struct):
    '''
    A IPC msg interchange format lib's encoder + decoder pair.

    '''
    # post-configure-cached when prop-accessed (see `mk_codec()`
    # OR can be passed directly as,
    # `MsgCodec(_enc=<Encoder>,  _dec=<Decoder>)`
    _enc: msgpack.Encoder|None = None
    _dec: msgpack.Decoder|None = None

    # struct type unions
    # https://jcristharif.com/msgspec/structs.html#tagged-unions
    @property
    def ipc_pld_spec(self) -> Union[Type[Struct]]:
        return self._dec.type

    lib: ModuleType = msgspec

    # ad-hoc type extensions
    # https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types
    enc_hook: Callable[[Any], Any]|None = None  # coder
    dec_hook: Callable[[type, Any], Any]|None = None # decoder

    # TODO: a sub-decoder system as well?
    # payload_msg_specs: Union[Type[Struct]] = Any
    # see related comments in `.msg.types`
    # _payload_decs: (
    #     dict[
    #         str,
    #         msgpack.Decoder,
    #     ]
    #     |None
    # ) = None
    # OR
    # ) = {
    #     # pre-seed decoders for std-py-type-set for use when
    #     # `Msg.pld == None|Any`.
    #     None: msgpack.Decoder(Any),
    #     Any: msgpack.Decoder(Any),
    # }

    # TODO: use `functools.cached_property` for these ?
    # https://docs.python.org/3/library/functools.html#functools.cached_property
    @property
    def enc(self) -> msgpack.Encoder:
        return self._enc

    def encode(
        self,
        py_obj: Any,

    ) -> bytes:
        '''
        Encode input python objects to `msgpack` bytes for transfer
        on a tranport protocol connection.

        '''
        return self._enc.encode(py_obj)

    @property
    def dec(self) -> msgpack.Decoder:
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
        return self._dec.decode(msg)

    # TODO: do we still want to try and support the sub-decoder with
    # `.Raw` technique in the case that the `Generic` approach gives
    # future grief?
    #
    # -[ ] <NEW-ISSUE-FOR-ThIS-HERE>
    #
    #def mk_pld_subdec(
    #    self,
    #    payload_types: Union[Type[Struct]],

    #) -> msgpack.Decoder:
    #    # TODO: sub-decoder suppor for `.pld: Raw`?
    #    # => see similar notes inside `.msg.types`..
    #    #
    #    # not sure we'll end up needing this though it might have
    #    # unforeseen advantages in terms of enabling encrypted
    #    # appliciation layer (only) payloads?
    #    #
    #    # register sub-payload decoders to load `.pld: Raw`
    #    # decoded `Msg`-packets using a dynamic lookup (table)
    #    # instead of a pre-defined msg-spec via `Generic`
    #    # parameterization.
    #    #
    #    (
    #        tags,
    #        payload_dec,
    #    ) = mk_tagged_union_dec(
    #        tagged_structs=list(payload_types.__args__),
    #    )
    #    # register sub-decoders by tag
    #    subdecs: dict[str, msgpack.Decoder]|None = self._payload_decs
    #    for name in tags:
    #        subdecs.setdefault(
    #            name,
    #            payload_dec,
    #        )

    #    return payload_dec

    # sub-decoders for retreiving embedded
    # payload data and decoding to a sender
    # side defined (struct) type.
    # def dec_payload(
    #     codec: MsgCodec,
    #     msg: Msg,

    # ) -> Any|Struct:

    #     msg: Msg = codec.dec.decode(msg)
    #     payload_tag: str = msg.header.payload_tag
    #     payload_dec: msgpack.Decoder = codec._payload_decs[payload_tag]
    #     return payload_dec.decode(msg.pld)

    # def enc_payload(
    #     codec: MsgCodec,
    #     payload: Any,
    #     cid: str,

    # ) -> bytes:

    #     # tag_field: str|None = None

    #     plbytes = codec.enc.encode(payload)
    #     if b'msg_type' in plbytes:
    #         assert isinstance(payload, Struct)

    #         # tag_field: str = type(payload).__name__
    #         payload = msgspec.Raw(plbytes)

    #     msg = Msg(
    #         cid=cid,
    #         pld=payload,
    #         # Header(
    #         #     payload_tag=tag_field,
    #         #     # dialog_id,
    #         # ),
    #     )
    #     return codec.enc.encode(msg)


 #def mk_tagged_union_dec(
    # tagged_structs: list[Struct],

 #) -> tuple[
    # list[str],
    # msgpack.Decoder,
 #]:
    # '''
    # Create a `msgpack.Decoder` for an input `list[msgspec.Struct]`
    # and return a `list[str]` of each struct's `tag_field: str` value
    # which can be used to "map to" the initialized dec.

    # '''
    # # See "tagged unions" docs:
    # # https://jcristharif.com/msgspec/structs.html#tagged-unions

    # # "The quickest way to enable tagged unions is to set tag=True when
    # # defining every struct type in the union. In this case tag_field
    # # defaults to "type", and tag defaults to the struct class name
    # # (e.g. "Get")."
    # first: Struct = tagged_structs[0]
    # types_union: Union[Type[Struct]] = Union[
    #    first
    # ]|Any
    # tags: list[str] = [first.__name__]

    # for struct in tagged_structs[1:]:
    #     types_union |= struct
    #     tags.append(
    #         getattr(
    #             struct,
    #             struct.__struct_config__.tag_field,
    #             struct.__name__,
    #         )
    #     )

    # dec = msgpack.Decoder(types_union)
    # return (
    #     tags,
    #     dec,
    # )


def mk_codec(
    ipc_msg_spec: Union[Type[Struct]]|Any|None = None,
    #
    # ^TODO^: in the long run, do we want to allow using a diff IPC `Msg`-set?
    # it would break the runtime, but maybe say if you wanted
    # to add some kinda field-specific or wholesale `.pld` ecryption?

    # struct type unions set for `Decoder`
    # https://jcristharif.com/msgspec/structs.html#tagged-unions
    ipc_pld_spec: Union[Type[Struct]]|Any|None = None,

    # TODO: offering a per-msg(-field) type-spec such that
    # the fields can be dynamically NOT decoded and left as `Raw`
    # values which are later loaded by a sub-decoder specified
    # by `tag_field: str` value key?
    # payload_msg_specs: dict[
    #     str,  # tag_field value as sub-decoder key
    #     Union[Type[Struct]]  # `Msg.pld` type spec
    # ]|None = None,

    libname: str = 'msgspec',

    # proxy as `Struct(**kwargs)`
    # ------ - ------
    dec_hook: Callable|None = None,
    enc_hook: Callable|None = None,
    # ------ - ------
    **kwargs,
    #
    # Encoder:
    # write_buffer_size=write_buffer_size,
    #
    # Decoder:
    # ext_hook: ext_hook_sig

) -> MsgCodec:
    '''
    Convenience factory for creating codecs eventually meant
    to be interchange lib agnostic (i.e. once we support more then just
    `msgspec` ;).

    '''
    if (
        ipc_msg_spec is not None
        and ipc_pld_spec
    ):
        raise RuntimeError(
            f'If a payload spec is provided,\n'
            "the builtin SC-shuttle-protocol's msg set\n"
            f'(i.e. `{Msg}`) MUST be used!\n\n'
            f'However both values were passed as => mk_codec(\n'
            f'   ipc_msg_spec={ipc_msg_spec}`\n'
            f'   ipc_pld_spec={ipc_pld_spec}`\n)\n'
        )

    elif (
        ipc_pld_spec
        and

        # XXX required for now (or maybe forever?) until
        # we can dream up a way to allow parameterizing and/or
        # custom overrides to the `Msg`-spec protocol itself?
        ipc_msg_spec is None
    ):
        # (manually) generate a msg-payload-spec for all relevant
        # god-boxing-msg subtypes, parameterizing the `Msg.pld: PayloadT`
        # for the decoder such that all sub-type msgs in our SCIPP
        # will automatically decode to a type-"limited" payload (`Struct`)
        # object (set).
        (
            ipc_msg_spec,
            msg_types,
        ) = mk_msg_spec(
            payload_type_union=ipc_pld_spec,
        )
        assert len(ipc_msg_spec.__args__) == len(msg_types)
        assert ipc_msg_spec

        dec = msgpack.Decoder(
            type=ipc_msg_spec,  # like `Msg[Any]`
        )

    else:
        ipc_msg_spec = ipc_msg_spec or Any

    enc = msgpack.Encoder(
       enc_hook=enc_hook,
    )
    dec = msgpack.Decoder(
        type=ipc_msg_spec,  # like `Msg[Any]`
        dec_hook=dec_hook,
    )

    codec = MsgCodec(
        _enc=enc,
        _dec=dec,
        # payload_msg_specs=payload_msg_specs,
        # **kwargs,
    )

    # sanity on expected backend support
    assert codec.lib.__name__ == libname

    return codec


# instance of the default `msgspec.msgpack` codec settings, i.e.
# no custom structs, hooks or other special types.
_def_msgspec_codec: MsgCodec = mk_codec(ipc_msg_spec=Any)

# NOTE: provides for per-`trio.Task` specificity of the
# IPC msging codec used by the transport layer when doing
# `Channel.send()/.recv()` of wire data.
_ctxvar_MsgCodec: ContextVar[MsgCodec] = ContextVar(
    'msgspec_codec',

    # TODO: move this to our new `Msg`-spec!
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
    # -> related to the `MsgCodec._payload_decs` stuff above..
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
