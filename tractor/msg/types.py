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
Define our strictly typed IPC message spec for the SCIPP:

that is,

the "Structurred-Concurrency-Inter-Process-(dialog)-(un)Protocol".

'''
from __future__ import annotations
import types
from typing import (
    Any,
    # Callable,
    Generic,
    Literal,
    Type,
    TypeVar,
    TypeAlias,
    Union,
)

from msgspec import (
    defstruct,
    # field,
    Raw,
    Struct,
    # UNSET,
    # UnsetType,
)

from tractor.msg import (
    pretty_struct,
)
from tractor.log import get_logger


log = get_logger('tractor.msgspec')

# type variable for the boxed payload field `.pld`
PayloadT = TypeVar('PayloadT')


class Msg(
    Struct,
    Generic[PayloadT],

    # https://jcristharif.com/msgspec/structs.html#tagged-unions
    tag=True,
    tag_field='msg_type',

    # https://jcristharif.com/msgspec/structs.html#field-ordering
    # kw_only=True,

    # https://jcristharif.com/msgspec/structs.html#equality-and-order
    # order=True,

    # https://jcristharif.com/msgspec/structs.html#encoding-decoding-as-arrays
    # as_array=True,
):
    '''
    The "god" boxing msg type.

    Boxes user data-msgs in a `.pld` and uses `msgspec`'s tagged
    unions support to enable a spec from a common msg inheritance
    tree.

    '''
    cid: str|None  # call/context-id
    # ^-TODO-^: more explicit type?
    # -[ ] use UNSET here?
    #  https://jcristharif.com/msgspec/supported-types.html#unset
    #
    # -[ ] `uuid.UUID` which has multi-protocol support
    #  https://jcristharif.com/msgspec/supported-types.html#uuid

    # The msgs "payload" (spelled without vowels):
    # https://en.wikipedia.org/wiki/Payload_(computing)
    #
    # NOTE: inherited from any `Msg` (and maybe overriden
    # by use of `limit_msg_spec()`), but by default is
    # parameterized to be `Any`.
    #
    # XXX this `Union` must strictly NOT contain `Any` if
    # a limited msg-type-spec is intended, such that when
    # creating and applying a new `MsgCodec` its 
    # `.decoder: Decoder` is configured with a `Union[Type[Struct]]` which
    # restricts the allowed payload content (this `.pld` field) 
    # by type system defined loading constraints B)
    #
    # TODO: could also be set to `msgspec.Raw` if the sub-decoders
    # approach is preferred over the generic parameterization 
    # approach as take by `mk_msg_spec()` below.
    pld: PayloadT|Raw


class Aid(
    Struct,
    tag=True,
    tag_field='msg_type',
):
    '''
    Actor-identity msg.

    Initial contact exchange enabling an actor "mailbox handshake"
    delivering the peer identity (and maybe eventually contact)
    info.

    Used by discovery protocol to register actors as well as
    conduct the initial comms (capability) filtering.

    '''
    name: str
    uuid: str
    # TODO: use built-in support for UUIDs?
    # -[ ] `uuid.UUID` which has multi-protocol support
    #  https://jcristharif.com/msgspec/supported-types.html#uuid


class SpawnSpec(
    pretty_struct.Struct,
    tag=True,
    tag_field='msg_type',
):
    '''
    Initial runtime spec handed down from a spawning parent to its
    child subactor immediately following first contact via an
    `Aid` msg.

    '''
    _parent_main_data: dict
    _runtime_vars: dict[str, Any]

    # module import capability
    enable_modules: dict[str, str]

    # TODO: not just sockaddr pairs?
    # -[ ] abstract into a `TransportAddr` type?
    reg_addrs: list[tuple[str, int]]
    bind_addrs: list[tuple[str, int]]



# TODO: caps based RPC support in the payload?
#
# -[ ] integration with our ``enable_modules: list[str]`` caps sys.
#   ``pkgutil.resolve_name()`` internally uses
#   ``importlib.import_module()`` which can be filtered by
#   inserting a ``MetaPathFinder`` into ``sys.meta_path`` (which
#   we could do before entering the ``Actor._process_messages()``
#   loop)?
#   - https://github.com/python/cpython/blob/main/Lib/pkgutil.py#L645
#   - https://stackoverflow.com/questions/1350466/preventing-python-code-from-importing-certain-modules
#   - https://stackoverflow.com/a/63320902
#   - https://docs.python.org/3/library/sys.html#sys.meta_path
#
# -[ ] can we combine .ns + .func into a native `NamespacePath` field?
#
# -[ ] better name, like `Call/TaskInput`?
#
# -[ ] XXX a debugger lock msg transaction with payloads like,
#   child -> `.pld: DebugLock` -> root
#   child <- `.pld: DebugLocked` <- root
#   child -> `.pld: DebugRelease` -> root
#
#   WHY => when a pld spec is provided it might not allow for
#   debug mode msgs as they currently are (using plain old `pld.
#   str` payloads) so we only when debug_mode=True we need to
#   union in this debugger payload set?
#
#   mk_msg_spec(
#       MyPldSpec,
#       debug_mode=True,
#   ) -> (
#       Union[MyPldSpec]
#      | Union[DebugLock, DebugLocked, DebugRelease]
#   )

# class Params(
#     Struct,
#     Generic[PayloadT],
# ):
#     spec: PayloadT|ParamSpec
#     inputs: InputsT|dict[str, Any]

    # TODO: for eg. we could stringently check the target
    # task-func's type sig and enforce it?
    # as an example for an IPTC,
    # @tractor.context
    # async def send_back_nsp(
    #     ctx: Context,
    #     expect_debug: bool,
    #     pld_spec_str: str,
    #     add_hooks: bool,
    #     started_msg_dict: dict,
    # ) -> <WhatHere!>:

    # TODO: figure out which of the `typing` feats we want to
    # support:
    # - plain ol `ParamSpec`:
    #   https://docs.python.org/3/library/typing.html#typing.ParamSpec
    # - new in 3.12 type parameter lists Bo
    # |_ https://docs.python.org/3/reference/compound_stmts.html#type-params
    # |_ historical pep 695: https://peps.python.org/pep-0695/
    # |_ full lang spec: https://typing.readthedocs.io/en/latest/spec/
    # |_ on annotation scopes:
    #    https://docs.python.org/3/reference/executionmodel.html#annotation-scopes
    # spec: ParamSpec[
    #     expect_debug: bool,
    #     pld_spec_str: str,
    #     add_hooks: bool,
    #     started_msg_dict: dict,
    # ]


# TODO: possibly sub-type for runtime method requests?
# -[ ] `Runtime(Start)` with a `.ns: str = 'self' or
#     we can just enforce any such method as having a strict
#     ns for calling funcs, namely the `Actor` instance?
class Start(
    Struct,
    tag=True,
    tag_field='msg_type',
):
    '''
    Initial request to remotely schedule an RPC `trio.Task` via
    `Actor.start_remote_task()`.

    It is called by all the following public APIs:

    - `ActorNursery.run_in_actor()`

    - `Portal.run()`
          `|_.run_from_ns()`
          `|_.open_stream_from()`
          `|_._submit_for_result()`

    - `Context.open_context()`

    '''
    cid: str

    ns: str
    func: str

    # TODO: make this a sub-struct which can be further
    # type-limited, maybe `Inputs`?
    # => SEE ABOVE <=
    kwargs: dict[str, Any]
    uid: tuple[str, str]  # (calling) actor-id

    # TODO: enforcing a msg-spec in terms `Msg.pld`
    # parameterizable msgs to be used in the appls IPC dialog.
    # => SEE `._codec.MsgDec` for more <=
    pld_spec: str = str(Any)


class StartAck(
    Struct,
    tag=True,
    tag_field='msg_type',
):
    '''
    Init response to a `Cmd` request indicating the far
    end's RPC spec, namely its callable "type".

    '''
    cid: str
    # TODO: maybe better names for all these?
    # -[ ] obvi ^ would need sync with `._rpc`
    functype: Literal[
        'asyncfunc',
        'asyncgen',
        'context',  # TODO: the only one eventually?
    ]

    # import typing
    # eval(str(Any), {}, {'typing': typing})
    # started_spec: str = str(Any)
    # return_spec


class Started(
    Msg,
    Generic[PayloadT],
):
    '''
    Packet to shuttle the "first value" delivered by
    `Context.started(value: Any)` from a `@tractor.context`
    decorated IPC endpoint.

    '''
    pld: PayloadT|Raw


# TODO: instead of using our existing `Start`
# for this (as we did with the original `{'cmd': ..}` style)
# class Cancel(Msg):
#     cid: str


class Yield(
    Msg,
    Generic[PayloadT],
):
    '''
    Per IPC transmission of a value from `await MsgStream.send(<value>)`.

    '''
    pld: PayloadT|Raw


class Stop(
    Struct,
    tag=True,
    tag_field='msg_type',
):
    '''
    Stream termination signal much like an IPC version 
    of `StopAsyncIteration`.

    '''
    cid: str
    # TODO: do we want to support a payload on stop?
    # pld: UnsetType = UNSET


# TODO: is `Result` or `Out[come]` a better name?
class Return(
    Msg,
    Generic[PayloadT],
):
    '''
    Final `return <value>` from a remotely scheduled
    func-as-`trio.Task`.

    '''
    pld: PayloadT|Raw


class CancelAck(
    Msg,
    Generic[PayloadT],
):
    '''
    Deliver the `bool` return-value from a cancellation `Actor`
    method scheduled via and prior RPC request.

    - `Actor.cancel()`
       `|_.cancel_soon()`
       `|_.cancel_rpc_tasks()`
       `|_._cancel_task()`
       `|_.cancel_server()`

    RPCs to these methods must **always** be able to deliver a result
    despite the currently configured IPC msg spec such that graceful
    cancellation is always functional in the runtime.

    '''
    pld: bool


# TODO: unify this with `._exceptions.RemoteActorError`
# such that we can have a msg which is both raisable and
# IPC-wire ready?
# B~o
class Error(
    Struct,
    tag=True,
    tag_field='msg_type',

    # TODO may omit defaults?
    # https://jcristharif.com/msgspec/structs.html#omitting-default-values
    # omit_defaults=True,
):
    '''
    A pkt that wraps `RemoteActorError`s for relay and raising.

    Fields are 1-to-1 meta-data as needed originally by
    `RemoteActorError.msgdata: dict` but now are defined here.

    Note: this msg shuttles `ContextCancelled` and `StreamOverrun`
    as well is used to rewrap any `MsgTypeError` for relay-reponse
    to bad `Yield.pld` senders during an IPC ctx's streaming dialog
    phase.

    '''
    src_uid: tuple[str, str]
    src_type_str: str
    boxed_type_str: str
    relay_path: list[tuple[str, str]]
    tb_str: str

    cid: str|None = None

    # TODO: use UNSET or don't include them via
    #
    # `ContextCancelled`
    canceller: tuple[str, str]|None = None

    # `StreamOverrun`
    sender: tuple[str, str]|None = None

    # for the `MsgTypeError` case where the receiver side
    # decodes the underlying original `Msg`-subtype
    _msg_dict: dict|None = None


def from_dict_msg(
    dict_msg: dict,

    msgT: MsgType|None = None,
    tag_field: str = 'msg_type',
    use_pretty: bool = False,

) -> MsgType:
    '''
    Helper to build a specific `MsgType` struct from
    a "vanilla" decoded `dict`-ified equivalent of the
    msg: i.e. if the `msgpack.Decoder.type == Any`.

    '''
    msg_type_tag_field: str = (
        msgT.__struct_config__.tag_field
        if msgT is not None
        else tag_field
    )
    # XXX ensure tag field is removed
    msgT_name: str = dict_msg.pop(msg_type_tag_field)
    msgT: MsgType = _msg_table[msgT_name]
    if use_pretty:
        msgT = defstruct(
            name=msgT_name,
            fields=[
                (key, fi.type)
                for fi, key, _
                in pretty_struct.iter_fields(msgT)
            ],
            bases=(
                pretty_struct.Struct,
                msgT,
            ),
        )
    return msgT(**dict_msg)

# TODO: should be make a msg version of `ContextCancelled?`
# and/or with a scope field or a full `ActorCancelled`?
# class Cancelled(Msg):
#     cid: str

# TODO what about overruns?
# class Overrun(Msg):
#     cid: str

_runtime_msgs: list[Msg] = [

    # identity handshake on first IPC `Channel` contact.
    Aid,

    # parent-to-child spawn specification passed as 2nd msg after
    # handshake ONLY after child connects back to parent.
    SpawnSpec,

    # inter-actor RPC initiation
    Start,  # schedule remote task-as-func
    StartAck,  # ack the schedule request

    # emission from `MsgStream.aclose()`
    Stop,

    # `Return` sub-type that we always accept from
    # runtime-internal cancel endpoints
    CancelAck,

    # box remote errors, normally subtypes
    # of `RemoteActorError`.
    Error,
]

# the no-outcome-yet IAC (inter-actor-communication) sub-set which
# can be `Msg.pld` payload field type-limited by application code
# using `apply_codec()` and `limit_msg_spec()`.
_payload_msgs: list[Msg] = [
    # first <value> from `Context.started(<value>)`
    Started,

    # any <value> sent via `MsgStream.send(<value>)`
    Yield,

    # the final value returned from a `@context` decorated
    # IPC endpoint.
    Return,
]

# built-in SC shuttle protocol msg type set in
# approx order of the IPC txn-state spaces.
__msg_types__: list[MsgType] = (
    _runtime_msgs
    +
    _payload_msgs
)


_msg_table: dict[str, MsgType] = {
    msgT.__name__: msgT
    for msgT in __msg_types__
}

# TODO: use new type declaration syntax for msg-type-spec
# https://docs.python.org/3/library/typing.html#type-aliases
# https://docs.python.org/3/reference/simple_stmts.html#type
MsgType: TypeAlias = Union[*__msg_types__]


def mk_msg_spec(
    payload_type_union: Union[Type] = Any,

    spec_build_method: Literal[
        'indexed_generics',  # works
        'defstruct',
        'types_new_class',

    ] = 'indexed_generics',

) -> tuple[
    Union[Type[Msg]],
    list[Type[Msg]],
]:
    '''
    Create a payload-(data-)type-parameterized IPC message specification.

    Allows generating IPC msg types from the above builtin set
    with a payload (field) restricted data-type via the `Msg.pld:
    PayloadT` type var. This allows runtime-task contexts to use
    the python type system to limit/filter payload values as
    determined by the input `payload_type_union: Union[Type]`.

    '''
    submsg_types: list[Type[Msg]] = Msg.__subclasses__()
    bases: tuple = (
        # XXX NOTE XXX the below generic-parameterization seems to
        # be THE ONLY way to get this to work correctly in terms
        # of getting ValidationError on a roundtrip?
        Msg[payload_type_union],
        Generic[PayloadT],
    )
    defstruct_bases: tuple = (
        Msg, # [payload_type_union],
        # Generic[PayloadT],
        # ^-XXX-^: not allowed? lul..
    )
    ipc_msg_types: list[Msg] = []

    idx_msg_types: list[Msg] = []
    defs_msg_types: list[Msg] = []
    nc_msg_types: list[Msg] = []

    for msgtype in __msg_types__:

        # for the NON-payload (user api) type specify-able
        # msgs types, we simply aggregate the def as is
        # for inclusion in the output type `Union`.
        if msgtype not in _payload_msgs:
            ipc_msg_types.append(msgtype)
            continue

        # check inheritance sanity
        assert msgtype in submsg_types

        # TODO: wait why do we need the dynamic version here?
        # XXX ANSWER XXX -> BC INHERITANCE.. don't work w generics..
        #
        # NOTE previously bc msgtypes WERE NOT inheritting
        # directly the `Generic[PayloadT]` type, the manual method
        # of generic-paraming with `.__class_getitem__()` wasn't
        # working..
        #
        # XXX but bc i changed that to make every subtype inherit
        # it, this manual "indexed parameterization" method seems
        # to work?
        #
        # -[x] paraming the `PayloadT` values via `Generic[T]`
        #   does work it seems but WITHOUT inheritance of generics
        #
        # -[-] is there a way to get it to work at module level
        #   just using inheritance or maybe a metaclass?
        #  => thot that `defstruct` might work, but NOPE, see
        #   below..
        #
        idxed_msg_type: Msg = msgtype[payload_type_union]
        idx_msg_types.append(idxed_msg_type)

        # TODO: WHY do we need to dynamically generate the
        # subtype-msgs here to ensure the `.pld` parameterization
        # propagates as well as works at all in terms of the
        # `msgpack.Decoder()`..?
        #
        # dynamically create the payload type-spec-limited msg set.
        newclass_msgtype: Type = types.new_class(
            name=msgtype.__name__,
            bases=bases,
            kwds={},
        )
        nc_msg_types.append(
            newclass_msgtype[payload_type_union]
        )

        # with `msgspec.structs.defstruct`
        # XXX ALSO DOESN'T WORK
        defstruct_msgtype = defstruct(
            name=msgtype.__name__,
            fields=[
                ('cid', str),

                # XXX doesn't seem to work..
                # ('pld', PayloadT),

                ('pld', payload_type_union),
            ],
            bases=defstruct_bases,
        )
        defs_msg_types.append(defstruct_msgtype)

        # assert index_paramed_msg_type == manual_paramed_msg_subtype

        # paramed_msg_type = manual_paramed_msg_subtype

        # ipc_payload_msgs_type_union |= index_paramed_msg_type

    idx_spec: Union[Type[Msg]] = Union[*idx_msg_types]
    def_spec: Union[Type[Msg]] = Union[*defs_msg_types]
    nc_spec: Union[Type[Msg]] = Union[*nc_msg_types]

    specs: dict[str, Union[Type[Msg]]] = {
        'indexed_generics': idx_spec,
        'defstruct': def_spec,
        'types_new_class': nc_spec,
    }
    msgtypes_table: dict[str, list[Msg]] = {
        'indexed_generics': idx_msg_types,
        'defstruct': defs_msg_types,
        'types_new_class': nc_msg_types,
    }

    # XXX lol apparently type unions can't ever
    # be equal eh?
    # TODO: grok the diff here better..
    #
    # assert (
    #     idx_spec
    #     ==
    #     nc_spec
    #     ==
    #     def_spec
    # )
    # breakpoint()

    pld_spec: Union[Type] = specs[spec_build_method]
    runtime_spec: Union[Type] = Union[*ipc_msg_types]
    ipc_spec = pld_spec | runtime_spec
    log.runtime(
        'Generating new IPC msg-spec\n'
        f'{ipc_spec}\n'
    )
    assert (
        ipc_spec
        and
        ipc_spec is not Any
    )
    return (
        ipc_spec,
        msgtypes_table[spec_build_method]
        +
        ipc_msg_types,
    )


# TODO: make something similar to this inside `._codec` such that
# user can just pass a type table of some sort?
# -[ ] we would need to decode all msgs to `pretty_struct.Struct`
#     and then call `.to_dict()` on them?
# -[ ] we're going to need to re-impl all the stuff changed in the
#    runtime port such that it can handle dicts or `Msg`s?
#
# def mk_dict_msg_codec_hooks() -> tuple[Callable, Callable]:
#     '''
#     Deliver a `enc_hook()`/`dec_hook()` pair which does
#     manual convertion from our above native `Msg` set
#     to `dict` equivalent (wire msgs) in order to keep legacy compat
#     with the original runtime implementation.
#
#     Note: this is is/was primarly used while moving the core
#     runtime over to using native `Msg`-struct types wherein we
#     start with the send side emitting without loading
#     a typed-decoder and then later flipping the switch over to
#     load to the native struct types once all runtime usage has
#     been adjusted appropriately.
#
#     '''
#     return (
#         # enc_to_dict,
#         dec_from_dict,
#     )
