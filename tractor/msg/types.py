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
    Generic,
    Literal,
    Type,
    TypeVar,
    Union,
)

from msgspec import (
    defstruct,
    # field,
    Struct,
    UNSET,
    UnsetType,
)

# type variable for the boxed payload field `.pld`
PayloadT = TypeVar('PayloadT')


class Msg(
    Struct,
    Generic[PayloadT],
    tag=True,
    tag_field='msg_type',

    # eq=True,
    # order=True,
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
    pld: PayloadT


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
# -[ ]better name, like `Call/TaskInput`?
#
class FuncSpec(Struct):
    ns: str
    func: str

    kwargs: dict
    uid: str  # (calling) actor-id


class Start(
    Msg,
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
    pld: FuncSpec


class IpcCtxSpec(Struct):
    '''
    An inter-actor-`trio.Task`-comms `Context` spec.

    '''
    # TODO: maybe better names for all these?
    # -[ ] obvi ^ would need sync with `._rpc`
    functype: Literal[
        'asyncfunc',
        'asyncgen',
        'context',  # TODO: the only one eventually?
    ]

    # TODO: as part of the reponse we should report our allowed
    # msg spec which should be generated from the type-annots as
    # desired in # https://github.com/goodboy/tractor/issues/365
    # When this does not match what the starter/caller side
    # expects we of course raise a `TypeError` just like if
    # a function had been called using an invalid signature.
    #
    # msgspec: MsgSpec


class StartAck(
    Msg,
    Generic[PayloadT],
):
    '''
    Init response to a `Cmd` request indicating the far
    end's RPC callable "type".

    '''
    pld: IpcCtxSpec


class Started(
    Msg,
    Generic[PayloadT],
):
    '''
    Packet to shuttle the "first value" delivered by
    `Context.started(value: Any)` from a `@tractor.context`
    decorated IPC endpoint.

    '''
    pld: PayloadT


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
    pld: PayloadT


class Stop(Msg):
    '''
    Stream termination signal much like an IPC version 
    of `StopAsyncIteration`.

    '''
    pld: UnsetType = UNSET


class Return(
    Msg,
    Generic[PayloadT],
):
    '''
    Final `return <value>` from a remotely scheduled
    func-as-`trio.Task`.

    '''
    pld: PayloadT


class ErrorData(Struct):
    '''
    Remote actor error meta-data as needed originally by
    `RemoteActorError.msgdata: dict`.

    '''
    src_uid: str
    src_type_str: str
    boxed_type_str: str

    relay_path: list[str]
    tb_str: str

    # `ContextCancelled`
    canceller: str|None = None

    # `StreamOverrun`
    sender: str|None = None


class Error(Msg):
    '''
    A pkt that wraps `RemoteActorError`s for relay.

    '''
    pld: ErrorData


# TODO: should be make a msg version of `ContextCancelled?`
# and/or with a scope field or a full `ActorCancelled`?
# class Cancelled(Msg):
#     cid: str

# TODO what about overruns?
# class Overrun(Msg):
#     cid: str


# built-in SC shuttle protocol msg type set in
# approx order of the IPC txn-state spaces.
__spec__: list[Msg] = [

    # inter-actor RPC initiation
    Start,
    StartAck,

    # no-outcome-yet IAC (inter-actor-communication)
    Started,
    Yield,
    Stop,

    # termination outcomes
    Return,
    Error,
]

_runtime_spec_msgs: list[Msg] = [
    Start,
    StartAck,
    Stop,
    Error,
]
_payload_spec_msgs: list[Msg] = [
    Started,
    Yield,
    Return,
]


def mk_msg_spec(
    payload_type_union: Union[Type] = Any,

    # boxing_msg_set: list[Msg] = _payload_spec_msgs,
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

    for msgtype in __spec__:

        # for the NON-payload (user api) type specify-able
        # msgs types, we simply aggregate the def as is
        # for inclusion in the output type `Union`.
        if msgtype not in _payload_spec_msgs:
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

    return (
        pld_spec | runtime_spec,
        msgtypes_table[spec_build_method] + ipc_msg_types,
    )
