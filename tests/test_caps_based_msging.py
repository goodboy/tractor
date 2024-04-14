'''
Low-level functional audits for our
"capability based messaging"-spec feats.

B~)

'''
import typing
from typing import (
    Any,
    Type,
    Union,
)
from contextvars import (
    Context,
)

from msgspec import (
    structs,
    msgpack,
    Struct,
    ValidationError,
)
import pytest

import tractor
from tractor import (
    _state,
    MsgTypeError,
)
from tractor.msg import (
    _codec,
    _ctxvar_MsgCodec,

    NamespacePath,
    MsgCodec,
    mk_codec,
    apply_codec,
    current_codec,
)
from tractor.msg.types import (
    _payload_msgs,
    log,
    Msg,
    Started,
    mk_msg_spec,
)
import trio


def mk_custom_codec(
    pld_spec: Union[Type]|Any,
    add_hooks: bool,

) -> MsgCodec:
    '''
    Create custom `msgpack` enc/dec-hooks and set a `Decoder`
    which only loads `pld_spec` (like `NamespacePath`) types.

    '''
    uid: tuple[str, str] = tractor.current_actor().uid

    # XXX NOTE XXX: despite defining `NamespacePath` as a type
    # field on our `Msg.pld`, we still need a enc/dec_hook() pair
    # to cast to/from that type on the wire. See the docs:
    # https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types

    def enc_nsp(obj: Any) -> Any:
        print(f'{uid} ENC HOOK')
        match obj:
            case NamespacePath():
                print(
                    f'{uid}: `NamespacePath`-Only ENCODE?\n'
                    f'obj-> `{obj}`: {type(obj)}\n'
                )
                # if type(obj) != NamespacePath:
                #     breakpoint()
                return str(obj)

        print(
            f'{uid}\n'
            'CUSTOM ENCODE\n'
            f'obj-arg-> `{obj}`: {type(obj)}\n'
        )
        logmsg: str = (
            f'{uid}\n'
            'FAILED ENCODE\n'
            f'obj-> `{obj}: {type(obj)}`\n'
        )
        raise NotImplementedError(logmsg)

    def dec_nsp(
        obj_type: Type,
        obj: Any,

    ) -> Any:
        print(
            f'{uid}\n'
            'CUSTOM DECODE\n'
            f'type-arg-> {obj_type}\n'
            f'obj-arg-> `{obj}`: {type(obj)}\n'
        )
        nsp = None

        if (
            obj_type is NamespacePath
            and isinstance(obj, str)
            and ':' in obj
        ):
            nsp = NamespacePath(obj)
            # TODO: we could built a generic handler using
            # JUST matching the obj_type part?
            # nsp = obj_type(obj)

        if nsp:
            print(f'Returning NSP instance: {nsp}')
            return nsp

        logmsg: str = (
            f'{uid}\n'
            'FAILED DECODE\n'
            f'type-> {obj_type}\n'
            f'obj-arg-> `{obj}`: {type(obj)}\n\n'
            f'current codec:\n'
            f'{current_codec()}\n'
        )
        # TODO: figure out the ignore subsys for this!
        # -[ ] option whether to defense-relay backc the msg
        #   inside an `Invalid`/`Ignore`
        # -[ ] how to make this handling pluggable such that a
        #   `Channel`/`MsgTransport` can intercept and process
        #   back msgs either via exception handling or some other
        #   signal?
        log.warning(logmsg)
        # NOTE: this delivers the invalid
        # value up to `msgspec`'s decoding
        # machinery for error raising.
        return obj
        # raise NotImplementedError(logmsg)

    nsp_codec: MsgCodec = mk_codec(
        ipc_pld_spec=pld_spec,

        # NOTE XXX: the encode hook MUST be used no matter what since
        # our `NamespacePath` is not any of a `Any` native type nor
        # a `msgspec.Struct` subtype - so `msgspec` has no way to know
        # how to encode it unless we provide the custom hook.
        #
        # AGAIN that is, regardless of whether we spec an
        # `Any`-decoded-pld the enc has no knowledge (by default)
        # how to enc `NamespacePath` (nsp), so we add a custom
        # hook to do that ALWAYS.
        enc_hook=enc_nsp if add_hooks else None,

        # XXX NOTE: pretty sure this is mutex with the `type=` to
        # `Decoder`? so it won't work in tandem with the
        # `ipc_pld_spec` passed above?
        dec_hook=dec_nsp if add_hooks else None,
    )
    return nsp_codec


def chk_codec_applied(
    expect_codec: MsgCodec,
    enter_value: MsgCodec|None = None,

) -> MsgCodec:
    '''
    buncha sanity checks ensuring that the IPC channel's
    context-vars are set to the expected codec and that are
    ctx-var wrapper APIs match the same.

    '''
    # TODO: play with tricyle again, bc this is supposed to work
    # the way we want?
    #
    # TreeVar
    # task: trio.Task = trio.lowlevel.current_task()
    # curr_codec = _ctxvar_MsgCodec.get_in(task)

    # ContextVar
    # task_ctx: Context = task.context
    # assert _ctxvar_MsgCodec in task_ctx
    # curr_codec: MsgCodec = task.context[_ctxvar_MsgCodec]

    # NOTE: currently we use this!
    # RunVar
    curr_codec: MsgCodec = current_codec()
    last_read_codec = _ctxvar_MsgCodec.get()
    # assert curr_codec is last_read_codec

    assert (
        (same_codec := expect_codec) is
        # returned from `mk_codec()`

        # yielded value from `apply_codec()`

        # read from current task's `contextvars.Context`
        curr_codec is
        last_read_codec

        # the default `msgspec` settings
        is not _codec._def_msgspec_codec
        is not _codec._def_tractor_codec
    )

    if enter_value:
        enter_value is same_codec


def iter_maybe_sends(
    send_items: dict[Union[Type], Any] | list[tuple],
    ipc_pld_spec: Union[Type] | Any,
    add_codec_hooks: bool,

    codec: MsgCodec|None = None,

) -> tuple[Any, bool]:

    if isinstance(send_items, dict):
        send_items = send_items.items()

    for (
        send_type_spec,
        send_value,
    ) in send_items:

        expect_roundtrip: bool = False

        # values-to-typespec santiy
        send_type = type(send_value)
        assert send_type == send_type_spec or (
            (subtypes := getattr(send_type_spec, '__args__', None))
            and send_type in subtypes
        )

        spec_subtypes: set[Union[Type]] = (
             getattr(
                 ipc_pld_spec,
                 '__args__',
                 {ipc_pld_spec,},
             )
        )
        send_in_spec: bool = (
            send_type == ipc_pld_spec
            or (
                ipc_pld_spec != Any
                and  # presume `Union` of types
                send_type in spec_subtypes
            )
            or (
                ipc_pld_spec == Any
                and
                send_type != NamespacePath
            )
        )
        expect_roundtrip = (
            send_in_spec
            # any spec should support all other
            # builtin py values that we send
            # except our custom nsp type which
            # we should be able to send as long
            # as we provide the custom codec hooks.
            or (
                ipc_pld_spec == Any
                and
                send_type == NamespacePath
                and
                add_codec_hooks
            )
        )

        if codec is not None:
            # XXX FIRST XXX ensure roundtripping works
            # before touching any IPC primitives/APIs.
            wire_bytes: bytes = codec.encode(
                Started(
                    cid='blahblah',
                    pld=send_value,
                )
            )
            # NOTE: demonstrates the decoder loading
            # to via our native SCIPP msg-spec
            # (structurred-conc-inter-proc-protocol)
            # implemented as per,
            try:
                msg: Started = codec.decode(wire_bytes)
                if not expect_roundtrip:
                    pytest.fail(
                        f'NOT-EXPECTED able to roundtrip value given spec:\n'
                        f'ipc_pld_spec -> {ipc_pld_spec}\n'
                        f'value -> {send_value}: {send_type}\n'
                    )

                pld = msg.pld
                assert pld == send_value

            except ValidationError:
                if expect_roundtrip:
                    pytest.fail(
                        f'EXPECTED to roundtrip value given spec:\n'
                        f'ipc_pld_spec -> {ipc_pld_spec}\n'
                        f'value -> {send_value}: {send_type}\n'
                    )

        yield (
            str(send_type),
            send_value,
            expect_roundtrip,
        )


def dec_type_union(
    type_names: list[str],
) -> Type:
    '''
    Look up types by name, compile into a list and then create and
    return a `typing.Union` from the full set.

    '''
    import importlib
    types: list[Type] = []
    for type_name in type_names:
        for ns in [
            typing,
            importlib.import_module(__name__),
        ]:
            if type_ref := getattr(
                ns,
                type_name,
                False,
            ):
                types.append(type_ref)

    # special case handling only..
    # ipc_pld_spec: Union[Type] = eval(
    #     pld_spec_str,
    #     {},  # globals
    #     {'typing': typing},  # locals
    # )

    return Union[*types]


def enc_type_union(
    union_or_type: Union[Type]|Type,
) -> list[str]:
    '''
    Encode a type-union or single type to a list of type-name-strings
    ready for IPC interchange.

    '''
    type_strs: list[str] = []
    for typ in getattr(
        union_or_type,
        '__args__',
        {union_or_type,},
    ):
        type_strs.append(typ.__qualname__)

    return type_strs


@tractor.context
async def send_back_values(
    ctx: Context,
    expect_debug: bool,
    pld_spec_type_strs: list[str],
    add_hooks: bool,
    started_msg_bytes: bytes,
    expect_ipc_send: dict[str, tuple[Any, bool]],

) -> None:
    '''
    Setup up a custom codec to load instances of `NamespacePath`
    and ensure we can round trip a func ref with our parent.

    '''
    uid: tuple = tractor.current_actor().uid

    # debug mode sanity check (prolly superfluous but, meh)
    assert expect_debug == _state.debug_mode()

    # init state in sub-actor should be default
    chk_codec_applied(
        expect_codec=_codec._def_tractor_codec,
    )

    # load pld spec from input str
    ipc_pld_spec = dec_type_union(
        pld_spec_type_strs,
    )
    pld_spec_str = str(ipc_pld_spec)

    # same as on parent side config.
    nsp_codec: MsgCodec = mk_custom_codec(
        pld_spec=ipc_pld_spec,
        add_hooks=add_hooks,
    )
    with (
        apply_codec(nsp_codec) as codec,
    ):
        chk_codec_applied(
            expect_codec=nsp_codec,
            enter_value=codec,
        )

        print(
            f'{uid}: attempting `Started`-bytes DECODE..\n'
        )
        try:
            msg: Started = nsp_codec.decode(started_msg_bytes)
            expected_pld_spec_str: str = msg.pld
            assert pld_spec_str == expected_pld_spec_str

        # TODO: maybe we should add our own wrapper error so as to
        # be interchange-lib agnostic?
        # -[ ] the error type is wtv is raised from the hook so we
        #   could also require a type-class of errors for
        #   indicating whether the hook-failure can be handled by
        #   a nasty-dialog-unprot sub-sys?
        except ValidationError:

            # NOTE: only in the `Any` spec case do we expect this to
            # work since otherwise no spec covers a plain-ol'
            # `.pld: str`
            if pld_spec_str == 'Any':
                raise
            else:
                print(
                    f'{uid}: (correctly) unable to DECODE `Started`-bytes\n'
                    f'{started_msg_bytes}\n'
                )

        iter_send_val_items = iter(expect_ipc_send.values())
        sent: list[Any] = []
        for send_value, expect_send in iter_send_val_items:
            try:
                print(
                    f'{uid}: attempting to `.started({send_value})`\n'
                    f'=> expect_send: {expect_send}\n'
                    f'SINCE, ipc_pld_spec: {ipc_pld_spec}\n'
                    f'AND, codec: {codec}\n'
                )
                await ctx.started(send_value)
                sent.append(send_value)
                if not expect_send:

                    # XXX NOTE XXX THIS WON'T WORK WITHOUT SPECIAL
                    # `str` handling! or special debug mode IPC
                    # msgs!
                    await tractor.pause()

                    raise RuntimeError(
                        f'NOT-EXPECTED able to roundtrip value given spec:\n'
                        f'ipc_pld_spec -> {ipc_pld_spec}\n'
                        f'value -> {send_value}: {type(send_value)}\n'
                    )

                break  # move on to streaming block..

            except tractor.MsgTypeError:
                await tractor.pause()

                if expect_send:
                    raise RuntimeError(
                        f'EXPECTED to `.started()` value given spec:\n'
                        f'ipc_pld_spec -> {ipc_pld_spec}\n'
                        f'value -> {send_value}: {type(send_value)}\n'
                    )

        async with ctx.open_stream() as ipc:
            print(
                f'{uid}: Entering streaming block to send remaining values..'
            )

            for send_value, expect_send in iter_send_val_items:
                send_type: Type = type(send_value)
                print(
                    '------ - ------\n'
                    f'{uid}: SENDING NEXT VALUE\n'
                    f'ipc_pld_spec: {ipc_pld_spec}\n'
                    f'expect_send: {expect_send}\n'
                    f'val: {send_value}\n'
                    '------ - ------\n'
                )
                try:
                    await ipc.send(send_value)
                    print(f'***\n{uid}-CHILD sent {send_value!r}\n***\n')
                    sent.append(send_value)

                    # NOTE: should only raise above on
                    # `.started()` or a `Return`
                    # if not expect_send:
                    #     raise RuntimeError(
                    #         f'NOT-EXPECTED able to roundtrip value given spec:\n'
                    #         f'ipc_pld_spec -> {ipc_pld_spec}\n'
                    #         f'value -> {send_value}: {send_type}\n'
                    #     )

                except ValidationError:
                    print(f'{uid} FAILED TO SEND {send_value}!')

                    # await tractor.pause()
                    if expect_send:
                        raise RuntimeError(
                            f'EXPECTED to roundtrip value given spec:\n'
                            f'ipc_pld_spec -> {ipc_pld_spec}\n'
                            f'value -> {send_value}: {send_type}\n'
                        )
                    # continue

            else:
                print(
                    f'{uid}: finished sending all values\n'
                    'Should be exiting stream block!\n'
                )

        print(f'{uid}: exited streaming block!')

        # TODO: this won't be true bc in streaming phase we DO NOT
        # msgspec check outbound msgs!
        # -[ ] once we implement the receiver side `InvalidMsg`
        #   then we can expect it here?
        # assert (
        #     len(sent)
        #     ==
        #     len([val
        #          for val, expect in
        #          expect_ipc_send.values()
        #          if expect is True])
        # )


def ex_func(*args):
    print(f'ex_func({args})')


@pytest.mark.parametrize(
    'ipc_pld_spec',
    [
        Any,
        NamespacePath,
        NamespacePath|None,  # the "maybe" spec Bo
    ],
    ids=[
        'any_type',
        'nsp_type',
        'maybe_nsp_type',
    ]
)
@pytest.mark.parametrize(
    'add_codec_hooks',
    [
        True,
        False,
    ],
    ids=['use_codec_hooks', 'no_codec_hooks'],
)
def test_codec_hooks_mod(
    debug_mode: bool,
    ipc_pld_spec: Union[Type]|Any,
    # send_value: None|str|NamespacePath,
    add_codec_hooks: bool,
):
    '''
    Audit the `.msg.MsgCodec` override apis details given our impl
    uses `contextvars` to accomplish per `trio` task codec
    application around an inter-proc-task-comms context.

    '''
    async def main():
        nsp = NamespacePath.from_ref(ex_func)
        send_items: dict[Union, Any] = {
            Union[None]: None,
            Union[NamespacePath]: nsp,
            Union[str]: str(nsp),
        }

        # init default state for actor
        chk_codec_applied(
            expect_codec=_codec._def_tractor_codec,
        )

        async with tractor.open_nursery(
            debug_mode=debug_mode,
        ) as an:
            p: tractor.Portal = await an.start_actor(
                'sub',
                enable_modules=[__name__],
            )

            # TODO: 2 cases:
            # - codec not modified -> decode nsp as `str`
            # - codec modified with hooks -> decode nsp as
            #   `NamespacePath`
            nsp_codec: MsgCodec = mk_custom_codec(
                pld_spec=ipc_pld_spec,
                add_hooks=add_codec_hooks,
            )
            with apply_codec(nsp_codec) as codec:
                chk_codec_applied(
                    expect_codec=nsp_codec,
                    enter_value=codec,
                )

                expect_ipc_send: dict[str, tuple[Any, bool]] = {}

                report: str = (
                    'Parent report on send values with\n'
                    f'ipc_pld_spec: {ipc_pld_spec}\n'
                    '       ------ - ------\n'
                )
                for val_type_str, val, expect_send in iter_maybe_sends(
                    send_items,
                    ipc_pld_spec,
                    add_codec_hooks=add_codec_hooks,
                ):
                    report += (
                        f'send_value: {val}: {type(val)} '
                        f'=> expect_send: {expect_send}\n'
                    )
                    expect_ipc_send[val_type_str] = (val, expect_send)

                print(
                    report +
                    '       ------ - ------\n'
                )
                assert len(expect_ipc_send) == len(send_items)
                # now try over real IPC with a the subactor
                # expect_ipc_rountrip: bool = True
                expected_started = Started(
                    cid='cid',
                    pld=str(ipc_pld_spec),
                )
                # build list of values we expect to receive from
                # the subactor.
                expect_to_send: list[Any] = [
                    val
                    for val, expect_send in expect_ipc_send.values()
                    if expect_send
                ]

                pld_spec_type_strs: list[str] = enc_type_union(ipc_pld_spec)

                # XXX should raise an mte (`MsgTypeError`)
                # when `add_codec_hooks == False` bc the input
                # `expect_ipc_send` kwarg has a nsp which can't be
                # serialized!
                #
                # TODO:can we ensure this happens from the
                # `Return`-side (aka the sub) as well?
                if not add_codec_hooks:
                    try:
                        async with p.open_context(
                            send_back_values,
                            expect_debug=debug_mode,
                            pld_spec_type_strs=pld_spec_type_strs,
                            add_hooks=add_codec_hooks,
                            started_msg_bytes=nsp_codec.encode(expected_started),

                            # XXX NOTE bc we send a `NamespacePath` in this kwarg
                            expect_ipc_send=expect_ipc_send,

                        ) as (ctx, first):
                            pytest.fail('ctx should fail to open without custom enc_hook!?')

                    # this test passes bc we can go no further!
                    except MsgTypeError:
                        # teardown nursery
                        await p.cancel_actor()
                        return

                # TODO: send the original nsp here and
                # test with `limit_msg_spec()` above?
                # await tractor.pause()
                print('PARENT opening IPC ctx!\n')
                async with (

                    # XXX should raise an mte (`MsgTypeError`)
                    # when `add_codec_hooks == False`..
                    p.open_context(
                        send_back_values,
                        expect_debug=debug_mode,
                        pld_spec_type_strs=pld_spec_type_strs,
                        add_hooks=add_codec_hooks,
                        started_msg_bytes=nsp_codec.encode(expected_started),
                        expect_ipc_send=expect_ipc_send,
                    ) as (ctx, first),

                    ctx.open_stream() as ipc,
                ):
                    # ensure codec is still applied across
                    # `tractor.Context` + its embedded nursery.
                    chk_codec_applied(
                        expect_codec=nsp_codec,
                        enter_value=codec,
                    )
                    print(
                        'root: ENTERING CONTEXT BLOCK\n'
                        f'type(first): {type(first)}\n'
                        f'first: {first}\n'
                    )
                    expect_to_send.remove(first)

                    # TODO: explicit values we expect depending on
                    # codec config!
                    # assert first == first_val
                    # assert first == f'{__name__}:ex_func'

                    async for next_sent in ipc:
                        print(
                            'Parent: child sent next value\n'
                            f'{next_sent}: {type(next_sent)}\n'
                        )
                        if expect_to_send:
                            expect_to_send.remove(next_sent)
                        else:
                            print('PARENT should terminate stream loop + block!')

                    # all sent values should have arrived!
                    assert not expect_to_send

            await p.cancel_actor()

    trio.run(main)


def chk_pld_type(
    payload_spec: Type[Struct]|Any,
    pld: Any,

    expect_roundtrip: bool|None = None,

) -> bool:

    pld_val_type: Type = type(pld)

    # TODO: verify that the overridden subtypes
    # DO NOT have modified type-annots from original!
    # 'Start',  .pld: FuncSpec
    # 'StartAck',  .pld: IpcCtxSpec
    # 'Stop',  .pld: UNSEt
    # 'Error',  .pld: ErrorData

    codec: MsgCodec = mk_codec(
        # NOTE: this ONLY accepts `Msg.pld` fields of a specified
        # type union.
        ipc_pld_spec=payload_spec,
    )

    # make a one-off dec to compare with our `MsgCodec` instance
    # which does the below `mk_msg_spec()` call internally
    ipc_msg_spec: Union[Type[Struct]]
    msg_types: list[Msg[payload_spec]]
    (
        ipc_msg_spec,
        msg_types,
    ) = mk_msg_spec(
        payload_type_union=payload_spec,
    )
    _enc = msgpack.Encoder()
    _dec = msgpack.Decoder(
        type=ipc_msg_spec or Any,  # like `Msg[Any]`
    )

    assert (
        payload_spec
        ==
        codec.pld_spec
    )

    # assert codec.dec == dec
    #
    # ^-XXX-^ not sure why these aren't "equal" but when cast
    # to `str` they seem to match ?? .. kk

    assert (
        str(ipc_msg_spec)
        ==
        str(codec.msg_spec)
        ==
        str(_dec.type)
        ==
        str(codec.dec.type)
    )

    # verify the boxed-type for all variable payload-type msgs.
    if not msg_types:
        breakpoint()

    roundtrip: bool|None = None
    pld_spec_msg_names: list[str] = [
        td.__name__ for td in _payload_msgs
    ]
    for typedef in msg_types:

        skip_runtime_msg: bool = typedef.__name__ not in pld_spec_msg_names
        if skip_runtime_msg:
            continue

        pld_field = structs.fields(typedef)[1]
        assert pld_field.type is payload_spec # TODO-^ does this need to work to get all subtypes to adhere?

        kwargs: dict[str, Any] = {
            'cid': '666',
            'pld': pld,
        }
        enc_msg: Msg = typedef(**kwargs)

        _wire_bytes: bytes = _enc.encode(enc_msg)
        wire_bytes: bytes = codec.enc.encode(enc_msg)
        assert _wire_bytes == wire_bytes

        ve: ValidationError|None = None
        try:
            dec_msg = codec.dec.decode(wire_bytes)
            _dec_msg = _dec.decode(wire_bytes)

            # decoded msg and thus payload should be exactly same!
            assert (roundtrip := (
                _dec_msg
                ==
                dec_msg
                ==
                enc_msg
            ))

            if (
                expect_roundtrip is not None
                and expect_roundtrip != roundtrip
            ):
                breakpoint()

            assert (
                pld
                ==
                dec_msg.pld
                ==
                enc_msg.pld
            )
            # assert (roundtrip := (_dec_msg == enc_msg))

        except ValidationError as _ve:
            ve = _ve
            roundtrip: bool = False
            if pld_val_type is payload_spec:
                raise ValueError(
                   'Got `ValidationError` despite type-var match!?\n'
                    f'pld_val_type: {pld_val_type}\n'
                    f'payload_type: {payload_spec}\n'
                ) from ve

            else:
                # ow we good cuz the pld spec mismatched.
                print(
                    'Got expected `ValidationError` since,\n'
                    f'{pld_val_type} is not {payload_spec}\n'
                )
        else:
            if (
                payload_spec is not Any
                and
                pld_val_type is not payload_spec
            ):
                raise ValueError(
                   'DID NOT `ValidationError` despite expected type match!?\n'
                    f'pld_val_type: {pld_val_type}\n'
                    f'payload_type: {payload_spec}\n'
                )

    # full code decode should always be attempted!
    if roundtrip is None:
        breakpoint()

    return roundtrip


def test_limit_msgspec():

    async def main():
        async with tractor.open_root_actor(
            debug_mode=True
        ):

            # ensure we can round-trip a boxing `Msg`
            assert chk_pld_type(
                # Msg,
                Any,
                None,
                expect_roundtrip=True,
            )

            # TODO: don't need this any more right since
            # `msgspec>=0.15` has the nice generics stuff yah??
            #
            # manually override the type annot of the payload
            # field and ensure it propagates to all msg-subtypes.
            # Msg.__annotations__['pld'] = Any

            # verify that a mis-typed payload value won't decode
            assert not chk_pld_type(
                # Msg,
                int,
                pld='doggy',
            )

            # parametrize the boxed `.pld` type as a custom-struct
            # and ensure that parametrization propagates
            # to all payload-msg-spec-able subtypes!
            class CustomPayload(Struct):
                name: str
                value: Any

            assert not chk_pld_type(
                # Msg,
                CustomPayload,
                pld='doggy',
            )

            assert chk_pld_type(
                # Msg,
                CustomPayload,
                pld=CustomPayload(name='doggy', value='urmom')
            )

            # uhh bc we can `.pause_from_sync()` now! :surfer:
            # breakpoint()

    trio.run(main)
