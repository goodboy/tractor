'''
Low-level functional audits for our
"capability based messaging"-spec feats.

B~)

'''
from typing import (
    Any,
    Type,
    Union,
)
from contextvars import (
    Context,
)
# from inspect import Parameter

from msgspec import (
    structs,
    msgpack,
    # defstruct,
    Struct,
    ValidationError,
)
import pytest
import tractor
from tractor.msg import (
    _codec,
    _ctxvar_MsgCodec,

    NamespacePath,
    MsgCodec,
    mk_codec,
    apply_codec,
    current_codec,
)
from tractor.msg import (
    types,
)
from tractor import _state
from tractor.msg.types import (
    # PayloadT,
    Msg,
    Started,
    mk_msg_spec,
)
import trio


def test_msg_spec_xor_pld_spec():
    '''
    If the `.msg.types.Msg`-set is overridden, we
    can't also support a `Msg.pld` spec.

    '''
    # apply custom hooks and set a `Decoder` which only
    # loads `NamespacePath` types.
    with pytest.raises(RuntimeError):
        mk_codec(
            ipc_msg_spec=Any,
            ipc_pld_spec=NamespacePath,
        )


def ex_func(*args):
    print(f'ex_func({args})')


def mk_custom_codec(
    pld_spec: Union[Type]|Any,

) -> MsgCodec:
    '''
    Create custom `msgpack` enc/dec-hooks and set a `Decoder`
    which only loads `NamespacePath` types.

    '''
    uid: tuple[str, str] = tractor.current_actor().uid

    # XXX NOTE XXX: despite defining `NamespacePath` as a type
    # field on our `Msg.pld`, we still need a enc/dec_hook() pair
    # to cast to/from that type on the wire. See the docs:
    # https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types

    def enc_nsp(obj: Any) -> Any:
        match obj:
            case NamespacePath():
                print(
                    f'{uid}: `NamespacePath`-Only ENCODE?\n'
                    f'type: {type(obj)}\n'
                    f'obj: {obj}\n'
                )

                return str(obj)

        logmsg: str = (
            f'{uid}: Encoding `{obj}: <{type(obj)}>` not supported'
            f'type: {type(obj)}\n'
            f'obj: {obj}\n'
        )
        print(logmsg)
        raise NotImplementedError(logmsg)

    def dec_nsp(
        type: Type,
        obj: Any,

    ) -> Any:
        print(
            f'{uid}: CUSTOM DECODE\n'
            f'input type: {type}\n'
            f'obj: {obj}\n'
            f'type(obj): `{type(obj).__class__}`\n'
        )
        nsp = None

        # This never seems to hit?
        if isinstance(obj, Msg):
            print(f'Msg type: {obj}')

        if (
            type is NamespacePath
            and isinstance(obj, str)
            and ':' in obj
        ):
            nsp = NamespacePath(obj)

        if nsp:
            print(f'Returning NSP instance: {nsp}')
            return nsp

        logmsg: str = (
            f'{uid}: Decoding `{obj}: <{type(obj)}>` not supported'
            f'input type: {type(obj)}\n'
            f'obj: {obj}\n'
            f'type(obj): `{type(obj).__class__}`\n'
        )
        print(logmsg)
        raise NotImplementedError(logmsg)


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
        enc_hook=enc_nsp,

        # XXX NOTE: pretty sure this is mutex with the `type=` to
        # `Decoder`? so it won't work in tandem with the
        # `ipc_pld_spec` passed above?
        dec_hook=dec_nsp,
    )
    return nsp_codec


@tractor.context
async def send_back_nsp(
    ctx: Context,
    expect_debug: bool,
    use_any_spec: bool,

) -> None:
    '''
    Setup up a custom codec to load instances of `NamespacePath`
    and ensure we can round trip a func ref with our parent.

    '''
    # debug mode sanity check
    assert expect_debug == _state.debug_mode()

    # task: trio.Task = trio.lowlevel.current_task()

    # TreeVar
    # curr_codec = _ctxvar_MsgCodec.get_in(task)

    # ContextVar
    # task_ctx: Context = task.context
    # assert _ctxvar_MsgCodec not in task_ctx

    curr_codec = _ctxvar_MsgCodec.get()
    assert curr_codec is _codec._def_tractor_codec

    if use_any_spec:
        pld_spec = Any
    else:
        # NOTE: don't need the |None here since
        # the parent side will never send `None` like
        # we do here in the implicit return at the end of this
        # `@context` body.
        pld_spec = NamespacePath  # |None

    nsp_codec: MsgCodec = mk_custom_codec(
        pld_spec=pld_spec,
    )
    with apply_codec(nsp_codec) as codec:
        chk_codec_applied(
            custom_codec=nsp_codec,
            enter_value=codec,
        )

        # ensure roundtripping works locally
        nsp = NamespacePath.from_ref(ex_func)
        wire_bytes: bytes = nsp_codec.encode(
            Started(
                cid=ctx.cid,
                pld=nsp
            )
        )
        msg: Started = nsp_codec.decode(wire_bytes)
        pld = msg.pld
        assert pld == nsp

        await ctx.started(nsp)
        async with ctx.open_stream() as ipc:
            async for msg in ipc:

                if use_any_spec:
                    assert msg == f'{__name__}:ex_func'

                    # TODO: as per below
                    # assert isinstance(msg, NamespacePath)
                    assert isinstance(msg, str)
                else:
                    assert isinstance(msg, NamespacePath)

                await ipc.send(msg)


def chk_codec_applied(
    custom_codec: MsgCodec,
    enter_value: MsgCodec,
) -> MsgCodec:

    # task: trio.Task = trio.lowlevel.current_task()

    # TreeVar
    # curr_codec = _ctxvar_MsgCodec.get_in(task)

    # ContextVar
    # task_ctx: Context = task.context
    # assert _ctxvar_MsgCodec in task_ctx
    # curr_codec: MsgCodec = task.context[_ctxvar_MsgCodec]

    # RunVar
    curr_codec: MsgCodec = _ctxvar_MsgCodec.get()
    last_read_codec = _ctxvar_MsgCodec.get()
    assert curr_codec is last_read_codec

    assert (
        # returned from `mk_codec()`
        custom_codec is

        # yielded value from `apply_codec()`
        enter_value is

        # read from current task's `contextvars.Context`
        curr_codec is

        # public API for all of the above
        current_codec()

        # the default `msgspec` settings
        is not _codec._def_msgspec_codec
        is not _codec._def_tractor_codec
    )


@pytest.mark.parametrize(
    'ipc_pld_spec',
    [
        # _codec._def_msgspec_codec,
        Any,
        # _codec._def_tractor_codec,
        NamespacePath|None,
    ],
    ids=[
        'any_type',
        'nsp_type',
    ]
)
def test_codec_hooks_mod(
    debug_mode: bool,
    ipc_pld_spec: Union[Type]|Any,
):
    '''
    Audit the `.msg.MsgCodec` override apis details given our impl
    uses `contextvars` to accomplish per `trio` task codec
    application around an inter-proc-task-comms context.

    '''
    async def main():

        # task: trio.Task = trio.lowlevel.current_task()

        # ContextVar
        # task_ctx: Context = task.context
        # assert _ctxvar_MsgCodec not in task_ctx

        # TreeVar
        # def_codec: MsgCodec = _ctxvar_MsgCodec.get_in(task)
        def_codec = _ctxvar_MsgCodec.get()
        assert def_codec is _codec._def_tractor_codec

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
            )
            with apply_codec(nsp_codec) as codec:
                chk_codec_applied(
                    custom_codec=nsp_codec,
                    enter_value=codec,
                )

                async with (
                    p.open_context(
                        send_back_nsp,
                        # TODO: send the original nsp here and
                        # test with `limit_msg_spec()` above?
                        expect_debug=debug_mode,
                        use_any_spec=(ipc_pld_spec==Any),

                    ) as (ctx, first),
                    ctx.open_stream() as ipc,
                ):
                    if ipc_pld_spec is NamespacePath:
                        assert isinstance(first, NamespacePath)

                    print(
                        'root: ENTERING CONTEXT BLOCK\n'
                        f'type(first): {type(first)}\n'
                        f'first: {first}\n'
                    )
                    # ensure codec is still applied across
                    # `tractor.Context` + its embedded nursery.
                    chk_codec_applied(
                        custom_codec=nsp_codec,
                        enter_value=codec,
                    )

                    first_nsp = NamespacePath(first)

                    # ensure roundtripping works
                    wire_bytes: bytes = nsp_codec.encode(
                        Started(
                            cid=ctx.cid,
                            pld=first_nsp
                        )
                    )
                    msg: Started = nsp_codec.decode(wire_bytes)
                    pld = msg.pld
                    assert  pld == first_nsp

                    # try a manual decode of the started msg+pld

                    # TODO: actually get the decoder loading
                    # to native once we spec our SCIPP msgspec
                    # (structurred-conc-inter-proc-protocol)
                    # implemented as per,
                    # https://github.com/goodboy/tractor/issues/36
                    #
                    if ipc_pld_spec is NamespacePath:
                        assert isinstance(first, NamespacePath)

                    # `Any`-payload-spec case
                    else:
                        assert isinstance(first, str)
                        assert first == f'{__name__}:ex_func'

                    await ipc.send(first)

                    with trio.move_on_after(.6):
                        async for msg in ipc:
                            print(msg)

                            # TODO: as per above
                            # assert isinstance(msg, NamespacePath)
                            assert isinstance(msg, str)
                            await ipc.send(msg)
                            await trio.sleep(0.1)

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
        td.__name__ for td in types._payload_spec_msgs
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
