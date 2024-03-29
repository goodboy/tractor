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
    Struct,
    UNSET,
)

# TODO: sub-decoded `Raw` fields?
# -[ ] see `MsgCodec._payload_decs` notes
#
# class Header(Struct, tag=True):
#     '''
#     A msg header which defines payload properties

#     '''
#     payload_tag: str|None = None

# type variable for the boxed payload field `.pld`
PayloadT = TypeVar('PayloadT')


class Msg(
    Struct,
    Generic[PayloadT],
    tag=True,
    tag_field='msg_type',
):
    '''
    The "god" boxing msg type.

    Boxes user data-msgs in a `.pld` and uses `msgspec`'s tagged
    unions support to enable a spec from a common msg inheritance
    tree.

    '''
    # TODO: use UNSET here?
    cid: str|None  # call/context-id

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


FuncType: Literal[
    'asyncfunc',
    'asyncgen',
    'context',  # TODO: the only one eventually?
] = 'context'


class IpcCtxSpec(Struct):
    '''
    An inter-actor-`trio.Task`-comms `Context` spec.

    '''
    functype: FuncType

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


class Stop(Msg):
    '''
    Stream termination signal much like an IPC version 
    of `StopAsyncIteration`.

    '''
    pld: UNSET


class Return(
    Msg,
    Generic[PayloadT],
):
    '''
    Final `return <value>` from a remotely scheduled
    func-as-`trio.Task`.

    '''


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


def mk_msg_spec(
    payload_type_union: Union[Type] = Any,
    boxing_msg_set: set[Msg] = {
        Started,
        Yield,
        Return,
    },

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

    # TODO: see below as well,
    # => union building approach with `.__class_getitem__()`
    # doesn't seem to work..?
    #
    # payload_type_spec: Union[Type[Msg]]
    #
    msg_types: list[Msg] = []
    for msgtype in boxing_msg_set:

        # check inheritance sanity
        assert msgtype in submsg_types

        # TODO: wait why do we need the dynamic version here?
        # -[ ] paraming the `PayloadT` values via `Generic[T]`
        #   doesn't seem to work at all?
        # -[ ] is there a way to get it to work at module level
        #   just using inheritance or maybe a metaclass?
        #
        # index_paramed_msg_type: Msg = msgtype[payload_type_union]

        # TODO: WHY do we need to dynamically generate the
        # subtype-msgs here to ensure the `.pld` parameterization
        # propagates as well as works at all in terms of the
        # `msgpack.Decoder()`..?
        #
        # dynamically create the payload type-spec-limited msg set.
        manual_paramed_msg_subtype: Type = types.new_class(
            msgtype.__name__,
            (
                # XXX NOTE XXX this seems to be THE ONLY
                # way to get this to work correctly!?!
                Msg[payload_type_union],
                Generic[PayloadT],
            ),
            {},
        )

        # TODO: grok the diff here better..
        # assert index_paramed_msg_type == manual_paramed_msg_subtype

        # XXX TODO: why does the manual method work but not the
        # `.__class_getitem__()` one!?!
        paramed_msg_type = manual_paramed_msg_subtype

        # payload_type_spec |= paramed_msg_type
        msg_types.append(paramed_msg_type)


    payload_type_spec: Union[Type[Msg]] = Union[*msg_types]
    return (
        payload_type_spec,
        msg_types,
    )
