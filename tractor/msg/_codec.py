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
from contextlib import (
    contextmanager as cm,
)
from contextvars import (
    ContextVar,
    Token,
)
import textwrap
from typing import (
    Any,
    Callable,
    Type,
    TYPE_CHECKING,
    Union,
)
from types import ModuleType

import msgspec
from msgspec import (
    msgpack,
    Raw,
)
# from trio.lowlevel import (
#     RunVar,
#     RunVarToken,
# )
# TODO: see notes below from @mikenerone..
# from tricycle import TreeVar

from tractor.msg.pretty_struct import Struct
from tractor.msg.types import (
    mk_msg_spec,
    MsgType,
)
from tractor.log import get_logger

if TYPE_CHECKING:
    from tractor._context import Context

log = get_logger(__name__)


# TODO: unify with `MsgCodec` by making `._dec` part this?
class MsgDec(Struct):
    '''
    An IPC msg decoder.

    Normally used to decode only a payload: `MsgType.pld:
    PayloadT` field before delivery to IPC consumer code.

    '''
    _dec: msgpack.Decoder

    @property
    def dec(self) -> msgpack.Decoder:
        return self._dec

    # struct type unions
    # https://jcristharif.com/msgspec/structs.html#tagged-unions
    #
    # ^-TODO-^: make a wrapper type for this such that alt
    # backends can be represented easily without a `Union` needed,
    # AND so that we have better support for wire transport.
    #
    # -[ ] maybe `FieldSpec` is a good name since msg-spec
    #   better applies to a `MsgType[FieldSpec]`?
    #
    # -[ ] both as part of the `.open_context()` call AND as part of the
    #     immediate ack-reponse (see similar below)
    #     we should do spec matching and fail if anything is awry?
    #
    # -[ ] eventually spec should be generated/parsed from the
    #     type-annots as # desired in GH issue:
    #     https://github.com/goodboy/tractor/issues/365
    #
    # -[ ] semantics of the mismatch case
    #   - when caller-callee specs we should raise
    #    a `MsgTypeError` or `MsgSpecError` or similar?
    #
    # -[ ] wrapper types for both spec types such that we can easily
    #     IPC transport them?
    #     - `TypeSpec: Union[Type]`
    #      * also a `.__contains__()` for doing `None in
    #      TypeSpec[None|int]` since rn you need to do it on
    #      `.__args__` for unions..
    #     - `MsgSpec: Union[Type[Msg]]
    #
    # -[ ] auto-genning this from new (in 3.12) type parameter lists Bo
    # |_ https://docs.python.org/3/reference/compound_stmts.html#type-params
    # |_ historical pep 695: https://peps.python.org/pep-0695/
    # |_ full lang spec: https://typing.readthedocs.io/en/latest/spec/
    # |_ on annotation scopes:
    #    https://docs.python.org/3/reference/executionmodel.html#annotation-scopes
    # |_ 3.13 will have subscriptable funcs Bo
    #    https://peps.python.org/pep-0718/
    @property
    def spec(self) -> Union[Type[Struct]]:
        # NOTE: defined and applied inside `mk_codec()`
        return self._dec.type

    # no difference, as compared to a `MsgCodec` which defines the
    # `MsgType.pld: PayloadT` part of its spec separately
    pld_spec = spec

    # TODO: would get moved into `FieldSpec.__str__()` right?
    @property
    def spec_str(self) -> str:

        # TODO: could also use match: instead?
        spec: Union[Type]|Type = self.spec

        # `typing.Union` case
        if getattr(spec, '__args__', False):
            return str(spec)

        # just a single type
        else:
            return spec.__name__

    pld_spec_str = spec_str

    def decode(
        self,
        raw: Raw|bytes,
    ) -> Any:
        return self._dec.decode(raw)

    @property
    def hook(self) -> Callable|None:
        return self._dec.dec_hook


def mk_dec(
    spec: Union[Type[Struct]]|Any = Any,
    dec_hook: Callable|None = None,

) -> MsgDec:

    return msgpack.Decoder(
        type=spec,  # like `Msg[Any]`
        dec_hook=dec_hook,
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

    Pretty much nothing more then delegation to underlying
    `msgspec.<interchange-protocol>.Encoder/Decoder`s for now.

    '''
    _enc: msgpack.Encoder
    _dec: msgpack.Decoder
    _pld_spec: Type[Struct]|Raw|Any

    def __repr__(self) -> str:
        speclines: str = textwrap.indent(
            self.pformat_msg_spec(),
            prefix=' '*3,
        )
        body: str = textwrap.indent(
            f'|_lib = {self.lib.__name__!r}\n'
            f'|_enc_hook: {self.enc.enc_hook}\n'
            f'|_dec_hook: {self.dec.dec_hook}\n'
            f'|_pld_spec: {self.pld_spec_str}\n'
            # f'|\n'
            f'|__msg_spec__:\n'
            f'{speclines}\n',
            prefix=' '*2,
        )
        return (
            f'<{type(self).__name__}(\n'
            f'{body}'
            ')>'
        )

    @property
    def pld_spec(self) -> Type[Struct]|Raw|Any:
        return self._pld_spec

    @property
    def pld_spec_str(self) -> str:

        # TODO: could also use match: instead?
        spec: Union[Type]|Type = self.pld_spec

        # `typing.Union` case
        if getattr(spec, '__args__', False):
            return str(spec)

        # just a single type
        else:
            return spec.__name__

    # struct type unions
    # https://jcristharif.com/msgspec/structs.html#tagged-unions
    @property
    def msg_spec(self) -> Union[Type[Struct]]:
        # NOTE: defined and applied inside `mk_codec()`
        return self._dec.type

    def msg_spec_items(
        self,
        msg: MsgType|None = None,

    ) -> dict[str, MsgType]|str:

        msgt_table: dict[str, MsgType] = {
            msgt: str(msgt)
            for msgt in self.msg_spec.__args__
        }
        if msg:
            msgt: MsgType = type(msg)
            str_repr: str = msgt_table[msgt]
            return {msgt: str_repr}

        return msgt_table

    # TODO: some way to make `pretty_struct.Struct` use this
    # wrapped field over the `.msg_spec` one?
    def pformat_msg_spec(
        self,
        msg: MsgType|None = None,
        join_char: str = '\n',
    ) -> str:
        return join_char.join(
            self.msg_spec_items(msg=msg).values()
        )

    lib: ModuleType = msgspec

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
        Encode input python objects to `msgpack` bytes for
        transfer on a tranport protocol connection.

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
        # https://jcristharif.com/msgspec/usage.html#typed-decoding
        return self._dec.decode(msg)

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
    #     # `MsgType.pld == None|Any`.
    #     None: msgpack.Decoder(Any),
    #     Any: msgpack.Decoder(Any),
    # }
    #
    # -[ ] do we still want to try and support the sub-decoder with
    # `.Raw` technique in the case that the `Generic` approach gives
    # future grief?
    #
    # -[ ] <NEW-ISSUE-FOR-ThIS-HERE>
    #  -> https://jcristharif.com/msgspec/api.html#raw
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



# TODO: sub-decoded `Raw` fields?
# -[ ] see `MsgCodec._payload_decs` notes
#
# XXX if we wanted something more complex then field name str-keys
# we might need a header field type to describe the lookup sys?
# class Header(Struct, tag=True):
#     '''
#     A msg header which defines payload properties

#     '''
#     payload_tag: str|None = None


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
    # struct type unions set for `Decoder`
    # https://jcristharif.com/msgspec/structs.html#tagged-unions
    ipc_pld_spec: Union[Type[Struct]]|Any = Any,

    # TODO: offering a per-msg(-field) type-spec such that
    # the fields can be dynamically NOT decoded and left as `Raw`
    # values which are later loaded by a sub-decoder specified
    # by `tag_field: str` value key?
    # payload_msg_specs: dict[
    #     str,  # tag_field value as sub-decoder key
    #     Union[Type[Struct]]  # `MsgType.pld` type spec
    # ]|None = None,

    libname: str = 'msgspec',

    # proxy as `Struct(**kwargs)` for ad-hoc type extensions
    # https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types
    # ------ - ------
    dec_hook: Callable|None = None,
    enc_hook: Callable|None = None,
    # ------ - ------
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

    # TODO: use this shim instead?
    # bc.. unification, err somethin?
    # dec: MsgDec = mk_dec(
    #     spec=ipc_msg_spec,
    #     dec_hook=dec_hook,
    # )

    dec = msgpack.Decoder(
        type=ipc_msg_spec,
        dec_hook=dec_hook,
    )
    enc = msgpack.Encoder(
       enc_hook=enc_hook,
    )

    codec = MsgCodec(
        _enc=enc,
        _dec=dec,
        _pld_spec=ipc_pld_spec,
    )

    # sanity on expected backend support
    assert codec.lib.__name__ == libname

    return codec


# instance of the default `msgspec.msgpack` codec settings, i.e.
# no custom structs, hooks or other special types.
_def_msgspec_codec: MsgCodec = mk_codec(ipc_pld_spec=Any)

# The built-in IPC `Msg` spec.
# Our composing "shuttle" protocol which allows `tractor`-app code
# to use any `msgspec` supported type as the `Msg.pld` payload,
# https://jcristharif.com/msgspec/supported-types.html
#
_def_tractor_codec: MsgCodec = mk_codec(
    # TODO: use this for debug mode locking prot?
    # ipc_pld_spec=Any,
    ipc_pld_spec=Raw,
)
# TODO: IDEALLY provides for per-`trio.Task` specificity of the
# IPC msging codec used by the transport layer when doing
# `Channel.send()/.recv()` of wire data.

# ContextVar-TODO: DIDN'T WORK, kept resetting in every new task to default!?
# _ctxvar_MsgCodec: ContextVar[MsgCodec] = ContextVar(

# TreeVar-TODO: DIDN'T WORK, kept resetting in every new embedded nursery
# even though it's supposed to inherit from a parent context ???
#
# _ctxvar_MsgCodec: TreeVar[MsgCodec] = TreeVar(
#
# ^-NOTE-^: for this to work see the mods by @mikenerone from `trio` gitter:
#
# 22:02:54 <mikenerone> even for regular contextvars, all you have to do is:
#    `task: Task = trio.lowlevel.current_task()`
#    `task.parent_nursery.parent_task.context.run(my_ctx_var.set, new_value)`
#
# From a comment in his prop code he couldn't share outright:
# 1. For every TreeVar set in the current task (which covers what
#    we need from SynchronizerFacade), walk up the tree until the
#    root or finding one where the TreeVar is already set, setting
#    it in all of the contexts along the way.
# 2. For each of those, we also forcibly set the values that are
#    pending for child nurseries that have not yet accessed the
#    TreeVar.
# 3. We similarly set the pending values for the child nurseries
#    of the *current* task.
#

# TODO: STOP USING THIS, since it's basically a global and won't
# allow sub-IPC-ctxs to limit the msg-spec however desired..
# _ctxvar_MsgCodec: MsgCodec = RunVar(
_ctxvar_MsgCodec: ContextVar[MsgCodec] = ContextVar(
    'msgspec_codec',
    default=_def_tractor_codec,
)


@cm
def apply_codec(
    codec: MsgCodec,

    ctx: Context|None = None,

) -> MsgCodec:
    '''
    Dynamically apply a `MsgCodec` to the current task's runtime
    context such that all (of a certain class of payload
    containing i.e. `MsgType.pld: PayloadT`) IPC msgs are
    processed with it for that task.

    Uses a `contextvars.ContextVar` to ensure the scope of any
    codec setting matches the current `Context` or
    `._rpc.process_messages()` feeder task's prior setting without
    mutating any surrounding scope.

    When a `ctx` is supplied, only mod its `Context.pld_codec`.

    matches the `@cm` block and DOES NOT change to the original
    (default) value in new tasks (as it does for `ContextVar`).

    '''
    __tracebackhide__: bool = True

    if ctx is not None:
        var: ContextVar = ctx._var_pld_codec
    else:
        # use IPC channel-connection "global" codec
        var: ContextVar = _ctxvar_MsgCodec

    orig: MsgCodec = var.get()

    assert orig is not codec
    if codec.pld_spec is None:
        breakpoint()

    log.info(
        'Applying new msg-spec codec\n\n'
        f'{codec}\n'
    )
    token: Token = var.set(codec)

    # ?TODO? for TreeVar approach which copies from the
    # cancel-scope of the prior value, NOT the prior task
    # See the docs:
    # - https://tricycle.readthedocs.io/en/latest/reference.html#tree-variables
    # - https://github.com/oremanj/tricycle/blob/master/tricycle/_tests/test_tree_var.py
    #   ^- see docs for @cm `.being()` API
    # with _ctxvar_MsgCodec.being(codec):
    #     new = _ctxvar_MsgCodec.get()
    #     assert new is codec
    #     yield codec

    try:
        yield var.get()
    finally:
        var.reset(token)
        log.info(
            'Reverted to last msg-spec codec\n\n'
            f'{orig}\n'
        )
        assert var.get() is orig


def current_codec() -> MsgCodec:
    '''
    Return the current `trio.Task.context`'s value
    for `msgspec_codec` used by `Channel.send/.recv()`
    for wire serialization.

    '''
    return _ctxvar_MsgCodec.get()


@cm
def limit_msg_spec(
    payload_spec: Union[Type[Struct]],

    # TODO: don't need this approach right?
    # -> related to the `MsgCodec._payload_decs` stuff above..
    # tagged_structs: list[Struct]|None = None,

    **codec_kwargs,

) -> MsgCodec:
    '''
    Apply a `MsgCodec` that will natively decode the SC-msg set's
    `Msg.pld: Union[Type[Struct]]` payload fields using
    tagged-unions of `msgspec.Struct`s from the `payload_types`
    for all IPC contexts in use by the current `trio.Task`.

    '''
    __tracebackhide__: bool = True
    curr_codec: MsgCodec = current_codec()
    msgspec_codec: MsgCodec = mk_codec(
        ipc_pld_spec=payload_spec,
        **codec_kwargs,
    )
    with apply_codec(msgspec_codec) as applied_codec:
        assert applied_codec is msgspec_codec
        yield msgspec_codec

    assert curr_codec is current_codec()


# XXX: msgspec won't allow this with non-struct custom types
# like `NamespacePath`!@!
# @cm
# def extend_msg_spec(
#     payload_spec: Union[Type[Struct]],

# ) -> MsgCodec:
#     '''
#     Extend the current `MsgCodec.pld_spec` (type set) by extending
#     the payload spec to **include** the types specified by
#     `payload_spec`.

#     '''
#     codec: MsgCodec = current_codec()
#     pld_spec: Union[Type] = codec.pld_spec
#     extended_spec: Union[Type] = pld_spec|payload_spec

#     with limit_msg_spec(payload_types=extended_spec) as ext_codec:
#         # import pdbp; pdbp.set_trace()
#         assert ext_codec.pld_spec == extended_spec
#         yield ext_codec
