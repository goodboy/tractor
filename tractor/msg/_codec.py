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
    TYPE_CHECKING,
    Any,
    Type,
    Union,
    Callable,
)
from types import ModuleType

import msgspec
from msgspec import (
    msgpack,
    Raw,
)
from msgspec.inspect import (
    CustomType,
    UnionType,
    SetType,
    ListType,
    TupleType
)
# TODO: see notes below from @mikenerone..
# from tricycle import TreeVar

from tractor.msg.pretty_struct import Struct
from tractor.msg.types import (
    mk_msg_spec,
    MsgType,
    PayloadMsg,
)
from tractor.log import get_logger

if TYPE_CHECKING:
    from tractor._context import Context

log = get_logger(__name__)


# TODO: unify with `MsgCodec` by making `._dec` part this?
class MsgDec(Struct):
    '''
    An IPC msg (payload) decoder.

    Normally used to decode only a payload: `MsgType.pld:
    PayloadT` field before delivery to IPC consumer code.

    '''
    _dec: msgpack.Decoder
    _ext_types_boxes: dict[Type, Struct] = {}

    @property
    def dec(self) -> msgpack.Decoder:
        return self._dec

    def __repr__(self) -> str:

        speclines: str = self.spec_str

        # in multi-typed spec case we stick the list
        # all on newlines after the |__pld_spec__:,
        # OW it's prolly single type spec-value
        # so just leave it on same line.
        if '\n' in speclines:
            speclines: str = '\n' + textwrap.indent(
                speclines,
                prefix=' '*3,
            )

        body: str = textwrap.indent(
            f'|_dec_hook: {self.dec.dec_hook}\n'
            f'|__pld_spec__: {speclines}\n',
            prefix=' '*2,
        )
        return (
            f'<{type(self).__name__}(\n'
            f'{body}'
            ')>'
        )

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
    #     - `MsgSpec: Union[MsgType]
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
        return pformat_msgspec(
            codec=self,
            join_char='|',
        )

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
    spec: Union[Type[Struct]]|Type|None,

    # NOTE, required for ad-hoc type extensions to the underlying
    # serialization proto (which is default `msgpack`),
    # https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types
    dec_hook: Callable|None = None,
    ext_types: list[Type]|None = None,

) -> MsgDec:
    '''
    Create an IPC msg decoder, a slightly higher level wrapper around
    a `msgspec.msgpack.Decoder` which provides,

    - easier introspection of the underlying type spec via
      the `.spec` and `.spec_str` attrs,
    - `.hook` access to the `Decoder.dec_hook()`,
    - automatic custom extension-types decode support when
      `dec_hook()` is provided such that any `PayloadMsg.pld` tagged
      as a type from from `ext_types` (presuming the `MsgCodec.encode()` also used
      a `.enc_hook()`) is processed and constructed by a `PldRx` implicitily.

    NOTE, as mentioned a `MsgDec` is normally used for `PayloadMsg.pld: PayloadT` field
    decoding inside an IPC-ctx-oriented `PldRx`.

    '''
    if (
        spec is None
        and
        ext_types is None
    ):
        raise TypeError(
            f'MIssing type-`spec` for msg decoder!\n'
            f'\n'
            f'`spec=None` is **only** permitted is if custom extension types '
            f'are provided via `ext_types`, meaning it must be non-`None`.\n'
            f'\n'
            f'In this case it is presumed that only the `ext_types`, '
            f'which much be handled by a paired `dec_hook()`, '
            f'will be permitted within the payload type-`spec`!\n'
            f'\n'
            f'spec = {spec!r}\n'
            f'dec_hook = {dec_hook!r}\n'
            f'ext_types = {ext_types!r}\n'
        )

    _boxed_structs: dict[Type, Struct] = {}

    if dec_hook:
        if ext_types is None:
            raise TypeError(
                f'If extending the serializable types with a custom decode hook (`dec_hook()`), '
                f'you must also provide the expected type set that the hook will handle '
                f'via a `ext_types: Union[Type]|None = None` argument!\n'
                f'\n'
                f'dec_hook = {dec_hook!r}\n'
                f'ext_types = {ext_types!r}\n'
            )

        if len(ext_types) > 1:
            _boxed_structs = mk_boxed_ext_structs(ext_types)
            ext_types = [
                etype
                for etype in ext_types
                if etype not in _boxed_structs
            ]
            ext_types += list(_boxed_structs.values())

        spec = Raw | Union[*ext_types]

    return MsgDec(
        _dec=msgpack.Decoder(
            type=spec,  # like `MsgType[Any]`
            dec_hook=dec_hook,
        ),
        _ext_types_boxes=_boxed_structs
    )


def mk_boxed_ext_structs(
    ext_types: list[Type],
) -> Struct:
    box_types: dict[Type, Struct] = {}
    for ext_type in ext_types:
        info = msgspec.inspect.type_info(ext_type)
        if isinstance(info, CustomType):
            box_types[ext_type] = msgspec.defstruct(
                f'Box{ext_type.__name__}',
                tag=True,
                fields=[
                    ('inner', ext_type),
                ],
            )

    return box_types


def unpack_spec_types(
    spec: Union[Type]|Type,
) -> set[Type]:
    '''
    Given an input type-`spec`, either a lone type
    or a `Union` of types (like `str|int|MyThing`),
    return a set of individual types.

    When `spec` is not a type-union returns `{spec,}`.

    '''
    spec_subtypes: set[Union[Type]] = set(
         getattr(
             spec,
             '__args__',
             {spec,},
         )
    )
    return spec_subtypes


def mk_msgspec_table(
    dec: msgpack.Decoder,
    msg: MsgType|None = None,

) -> dict[str, MsgType]|str:
    '''
    Fill out a `dict` of `MsgType`s keyed by name
    for a given input `msgspec.msgpack.Decoder`
    as defined by its `.type: Union[Type]` setting.

    If `msg` is provided, only deliver a `dict` with a single
    entry for that type.

    '''
    msgspec: Union[Type]|Type = dec.type

    if not (msgtypes := getattr(msgspec, '__args__', False)):
        msgtypes = [msgspec]

    msgt_table: dict[str, MsgType] = {
        msgt: str(msgt.__name__)
        for msgt in msgtypes
    }
    if msg:
        msgt: MsgType = type(msg)
        str_repr: str = msgt_table[msgt]
        return {msgt: str_repr}

    return msgt_table


def pformat_msgspec(
    codec: MsgCodec|MsgDec,
    msg: MsgType|None = None,
    join_char: str = '\n',

) -> str:
    '''
    Pretty `str` format the `msgspec.msgpack.Decoder.type` attribute
    for display in (console) log messages as a nice (maybe multiline)
    presentation of all supported `Struct`s (subtypes) available for
    typed decoding.

    '''
    dec: msgpack.Decoder = getattr(codec, 'dec', codec)
    return join_char.join(
        mk_msgspec_table(
            dec=dec,
            msg=msg,
        ).values()
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

    _ext_types_boxes: dict[Type, Struct] = {}

    def __repr__(self) -> str:
        speclines: str = textwrap.indent(
            pformat_msgspec(codec=self),
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

    # TODO: some way to make `pretty_struct.Struct` use this
    # wrapped field over the `.msg_spec` one?
    @property
    def msg_spec_str(self) -> str:
        return pformat_msgspec(self.msg_spec)

    lib: ModuleType = msgspec

    # TODO: use `functools.cached_property` for these ?
    # https://docs.python.org/3/library/functools.html#functools.cached_property
    @property
    def enc(self) -> msgpack.Encoder:
        return self._enc

    # TODO: reusing encode buffer for perf?
    # https://jcristharif.com/msgspec/perf-tips.html#reusing-an-output-buffer
    _buf: bytearray = bytearray()

    def encode(
        self,
        py_obj: Any|PayloadMsg,

        use_buf: bool = False,
        # ^-XXX-^ uhh why am i getting this?
        # |_BufferError: Existing exports of data: object cannot be re-sized

        as_ext_type: bool = False,
        hide_tb: bool = True,

    ) -> bytes:
        '''
        Encode input python objects to `msgpack` bytes for
        transfer on a tranport protocol connection.

        When `use_buf == True` use the output buffer optimization:
        https://jcristharif.com/msgspec/perf-tips.html#reusing-an-output-buffer

        '''
        __tracebackhide__: bool = hide_tb

        try:

            box: Struct|None = self._ext_types_boxes.get(type(py_obj), None)
            if (
                as_ext_type
                or
                box
            ):
                py_obj = box(inner=py_obj)

            if use_buf:
                self._enc.encode_into(py_obj, self._buf)
                return self._buf

            return self._enc.encode(py_obj)

        except TypeError as typerr:
            typerr.add_note(
                '|_src error from `msgspec`'
                # f'|_{self._enc.encode!r}'
            )
            raise typerr

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


# ?TODO? time to remove this finally?
#
# -[x] TODO: a sub-decoder system as well?
# => No! already re-architected to include a "payload-receiver"
#   now found in `._ops`.
#
# -[x] do we still want to try and support the sub-decoder with
# `.Raw` technique in the case that the `Generic` approach gives
# future grief?
# => well YES but NO, since we went with the `PldRx` approach
#   instead!
#
# IF however you want to see the code that was staged for this
# from wayyy back, see the pure removal commit.


def mk_codec(
    ipc_pld_spec: Union[Type[Struct]]|Any|Raw = Raw,
    # tagged-struct-types-union set for `Decoder`ing of payloads, as
    # per https://jcristharif.com/msgspec/structs.html#tagged-unions.
    # NOTE that the default `Raw` here **is very intentional** since
    # the `PldRx._pld_dec: MsgDec` is responsible for per ipc-ctx-task
    # decoding of msg-specs defined by the user as part of **their**
    # `tractor` "app's" type-limited IPC msg-spec.

    # TODO: offering a per-msg(-field) type-spec such that
    # the fields can be dynamically NOT decoded and left as `Raw`
    # values which are later loaded by a sub-decoder specified
    # by `tag_field: str` value key?
    # payload_msg_specs: dict[
    #     str,  # tag_field value as sub-decoder key
    #     Union[Type[Struct]]  # `MsgType.pld` type spec
    # ]|None = None,

    libname: str = 'msgspec',

    # settings for encoding-to-send extension-types,
    # https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types
    # dec_hook: Callable|None = None,
    enc_hook: Callable|None = None,
    ext_types: list[Type]|None = None,

    # ?TODO? other params we might want to support
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
    pld_spec = ipc_pld_spec
    if enc_hook:
        if not ext_types:
            raise TypeError(
                f'If extending the serializable types with a custom encode hook (`enc_hook()`), '
                f'you must also provide the expected type set that the hook will handle '
                f'via a `ext_types: Union[Type]|None = None` argument!\n'
                f'\n'
                f'enc_hook = {enc_hook!r}\n'
                f'ext_types = {ext_types!r}\n'
            )

    dec_hook: Callable|None = None

    # (manually) generate a msg-spec (how appropes) for all relevant
    # payload-boxing-struct-msg-types, parameterizing the
    # `PayloadMsg.pld: PayloadT` for the decoder such that all msgs
    # in our SC-RPC-protocol will automatically decode to
    # a type-"limited" payload (`Struct`) object (set).
    (
        ipc_msg_spec,
        msg_types,
    ) = mk_msg_spec(
        payload_type_union=pld_spec,
    )

    msg_spec_types: set[Type] = unpack_spec_types(ipc_msg_spec)
    assert (
        len(ipc_msg_spec.__args__) == len(msg_types)
        and
        len(msg_spec_types) == len(msg_types)
    )

    dec = msgpack.Decoder(
        type=ipc_msg_spec,
        dec_hook=dec_hook,
    )
    enc = msgpack.Encoder(
       enc_hook=enc_hook,
    )

    boxes = {}
    if ext_types and len(ext_types) > 1:
        boxes = mk_boxed_ext_structs(ext_types)

    codec = MsgCodec(
        _enc=enc,
        _dec=dec,
        _pld_spec=pld_spec,
        _ext_types_boxes=boxes
    )
    # sanity on expected backend support
    assert codec.lib.__name__ == libname
    return codec


# instance of the default `msgspec.msgpack` codec settings, i.e.
# no custom structs, hooks or other special types.
#
# XXX NOTE XXX, this will break our `Context.start()` call!
#
# * by default we roundtrip the started pld-`value` and if you apply
#   this codec (globally anyway with `apply_codec()`) then the
#   `roundtripped` value will include a non-`.pld: Raw` which will
#   then type-error on the consequent `._ops.validte_payload_msg()`..
#
_def_msgspec_codec: MsgCodec = mk_codec(
    ipc_pld_spec=Any,
)

# The built-in IPC `Msg` spec.
# Our composing "shuttle" protocol which allows `tractor`-app code
# to use any `msgspec` supported type as the `PayloadMsg.pld` payload,
# https://jcristharif.com/msgspec/supported-types.html
#
_def_tractor_codec: MsgCodec = mk_codec(
    ipc_pld_spec=Raw,  # XXX should be default righ!?
)

# -[x] TODO, IDEALLY provides for per-`trio.Task` specificity of the
# IPC msging codec used by the transport layer when doing
# `Channel.send()/.recv()` of wire data.
# => impled as our `PldRx` which is `Context` scoped B)

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

    try:
        yield var.get()
    finally:
        var.reset(token)
        log.info(
            'Reverted to last msg-spec codec\n\n'
            f'{orig}\n'
        )
        assert var.get() is orig

    # ?TODO? for TreeVar approach which copies from the
    # cancel-scope of the prior value, NOT the prior task
    #
    # See the docs:
    # - https://tricycle.readthedocs.io/en/latest/reference.html#tree-variables
    # - https://github.com/oremanj/tricycle/blob/master/tricycle/_tests/test_tree_var.py
    #   ^- see docs for @cm `.being()` API
    #
    # with _ctxvar_MsgCodec.being(codec):
    #     new = _ctxvar_MsgCodec.get()
    #     assert new is codec
    #     yield codec


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

    hide_tb: bool = True,
    **codec_kwargs,

) -> MsgCodec:
    '''
    Apply a `MsgCodec` that will natively decode the SC-msg set's
    `PayloadMsg.pld: Union[Type[Struct]]` payload fields using
    tagged-unions of `msgspec.Struct`s from the `payload_types`
    for all IPC contexts in use by the current `trio.Task`.

    '''
    __tracebackhide__: bool = hide_tb
    curr_codec: MsgCodec = current_codec()
    msgspec_codec: MsgCodec = mk_codec(
        ipc_pld_spec=payload_spec,
        **codec_kwargs,
    )
    with apply_codec(msgspec_codec) as applied_codec:
        assert applied_codec is msgspec_codec
        yield msgspec_codec

    assert curr_codec is current_codec()


'''
Encoder / Decoder generic hook factory

'''


# builtins we can have in same pld_spec as custom types
default_builtins = (
    None,
    bool,
    int,
    float,
    bytes,
    list
)


# spec definition type
TypeSpec = (
    Type |
    Union[Type] |
    list[Type] |
    tuple[Type] |
    set[Type]
)


class TypeCodec:
    '''
    This class describes a way of encoding to or decoding from a "wire type",
    objects that have `encode_fn` and `decode_fn` can be used with
    `.encode/.decode`.

    '''

    def __init__(
        self,
        wire_type: Type,
        decode_fn: str,
        encode_fn: str = 'encode',
    ):
        self._encode_fn: str = encode_fn
        self._decode_fn: str = decode_fn
        self._wire_type: Type = wire_type

    @property
    def encode_fn(self) -> str:
        return self._encode_fn

    @property
    def decode_fn(self) -> str:
        return self._decode_fn

    @property
    def wire_type(self) -> str:
        return self._wire_type

    def is_type_compat(self, obj: any) -> bool:
        return (
            hasattr(obj, self._encode_fn)
            and
            hasattr(obj, self._decode_fn)
        )

    def encode(self, obj: any) -> any:
        return getattr(obj, self._encode_fn)(obj)

    def decode(self, cls: Type, raw: any) -> any:
        return getattr(cls, self._decode_fn)(raw)


'''
Default codec descriptions for wire types:

    - bytes
    - str
    - int

'''


BytesCodec = TypeCodec(
    decode_fn='from_bytes',
    wire_type=bytes
)


StrCodec = TypeCodec(
    decode_fn='from_str',
    wire_type=str
)


IntCodec = TypeCodec(
    decode_fn='from_int',
    wire_type=int
)


default_codecs: dict[Type, TypeCodec] = {
    bytes: BytesCodec,
    str: StrCodec,
    int: IntCodec
}


def mk_spec_set(
    spec: TypeSpec
) -> set[Type]:
    '''
    Given any of the different spec definitions, always return a `set[Type]`
    with each spec type as an item.

    - When passed list|tuple|set do nothing
    - When passed a single type we wrap it in tuple
    - When passed a Union we wrap its inner types in tuple

    '''
    if not (
        isinstance(spec, set)
        or
        isinstance(spec, list)
        or
        isinstance(spec, tuple)
    ):
        spec_info = msgspec.inspect.type_info(spec)
        match spec_info:
            case UnionType():
                return set((
                    t.cls
                    for t in spec_info.types
                ))

            case (
                SetType() |
                ListType() |
                TupleType()
            ):
                return set((spec_info.item_type, ))

            case _:
                return set((spec, ))

    return set(spec)


def mk_codec_map_from_spec(
    spec: TypeSpec,
    codecs: dict[Type, TypeCodec] = default_codecs
) -> dict[Type, TypeCodec]:
    '''
    Generate a map of spec type -> supported codec

    '''

    spec: set[Type] = mk_spec_set(spec)

    spec_codecs: dict[Type, TypeCodec] = {}
    for t in spec:
        for codec in codecs.values():
            if codec.is_type_compat(t):
                spec_codecs[t] = codec

    return spec_codecs


def mk_enc_hook(
    spec: TypeSpec,
    with_builtins: bool = True,
    builtins: set[Type] = default_builtins,
    codecs: dict[Type, TypeCodec] = default_codecs
) -> Callable:
    '''
    Given a type specification return a msgspec enc_hook fn

    '''
    spec_codecs = mk_codec_map_from_spec(spec)

    def enc_hook(obj: any) -> any:
        t = type(obj)
        maybe_codec = spec_codecs.get(t, None)
        if maybe_codec:
            return maybe_codec.encode(obj)

        # passthrough built ins
        if builtins and t in builtins:
            return obj

        raise NotImplementedError(
            f"Objects of type {type(obj)} are not supported:\n{obj}"
        )

    return enc_hook


def mk_dec_hook(
    spec: TypeSpec,
    with_builtins: bool = True,
    builtins: set[Type] = default_builtins,
    codecs: dict[Type, TypeCodec] = default_codecs
) -> Callable:
    '''
    Given a type specification return a msgspec dec_hook fn

    '''
    spec_codecs = mk_codec_map_from_spec(spec)

    def dec_hook(t: Type, obj: any) -> any:
        maybe_codec = spec_codecs.get(t, None)
        if maybe_codec:
            return maybe_codec.decode(t, obj)

        # passthrough builtins
        if builtins and type in builtins:
            return obj

        raise NotImplementedError(
            f"Objects of type {type} are not supported from {obj}"
        )

    return dec_hook


def mk_codec_hooks(*args, **kwargs) -> tuple[Callable, Callable]:
    '''
    Given a type specification return a msgspec enc & dec hook fn pair

    '''
    return (
        mk_enc_hook(*args, **kwargs),

        mk_dec_hook(*args, **kwargs)
    )


def mk_codec_from_spec(
    spec: TypeSpec,
    with_builtins: bool = True,
    builtins: set[Type] = default_builtins,
    codecs: dict[Type, TypeCodec] = default_codecs
) -> MsgCodec:
    '''
    Given a type specification return a MsgCodec

    '''
    spec: set[Type] = mk_spec_set(spec)

    return mk_codec(
        enc_hook=mk_enc_hook(
            spec,
            with_builtins=with_builtins,
            builtins=builtins,
            codecs=codecs
        ),
        ext_types=spec
    )


def mk_msgpack_codec(
    spec: TypeSpec,
    with_builtins: bool = True,
    builtins: set[Type] = default_builtins,
    codecs: dict[Type, TypeCodec] = default_codecs
) -> tuple[msgpack.Encoder, msgpack.Decoder]:
    '''
    Get a msgpack Encoder, Decoder pair for a given type spec

    '''
    spec: set[Type] = mk_spec_set(spec)

    enc_hook, dec_hook = mk_codec_hooks(
        spec,
        with_builtins=with_builtins,
        builtins=builtins,
        codecs=codecs
    )
    encoder = msgpack.Encoder(enc_hook=enc_hook)
    decoder = msgpack.Decoder(spec, dec_hook=dec_hook)
    return encoder, decoder
