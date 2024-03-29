'''
Low-level functional audits for our
"capability based messaging"-spec feats.

B~)

'''
from typing import (
    Any,
    _GenericAlias,
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
    _def_msgspec_codec,
    _ctxvar_MsgCodec,

    NamespacePath,
    MsgCodec,
    mk_codec,
    apply_codec,
    current_msgspec_codec,
)
from tractor.msg.types import (
    # PayloadT,
    Msg,
    # Started,
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


# TODO: wrap these into `._codec` such that user can just pass
# a type table of some sort?
def enc_hook(obj: Any) -> Any:
    if isinstance(obj, NamespacePath):
        return str(obj)
    else:
        raise NotImplementedError(
            f'Objects of type {type(obj)} are not supported'
        )


def dec_hook(type: Type, obj: Any) -> Any:
    print(f'type is: {type}')
    if type is NamespacePath:
        return NamespacePath(obj)
    else:
        raise NotImplementedError(
            f'Objects of type {type(obj)} are not supported'
        )


def ex_func(*args):
    print(f'ex_func({args})')


def mk_custom_codec(
    ipc_msg_spec: Type[Any] = Any,
) -> MsgCodec:
    # apply custom hooks and set a `Decoder` which only
    # loads `NamespacePath` types.
    nsp_codec: MsgCodec = mk_codec(
        ipc_msg_spec=ipc_msg_spec,
        enc_hook=enc_hook,
        dec_hook=dec_hook,
    )

    # TODO: validate `MsgCodec` interface/semantics?
    # -[ ] simple field tests to ensure caching + reset is workin?
    # -[ ] custom / changing `.decoder()` calls?
    #
    # dec = nsp_codec.decoder(
    #     types=NamespacePath,
    # )
    # assert nsp_codec.dec is dec
    return nsp_codec


@tractor.context
async def send_back_nsp(
    ctx: tractor.Context,

) -> None:
    '''
    Setup up a custom codec to load instances of `NamespacePath`
    and ensure we can round trip a func ref with our parent.

    '''
    task: trio.Task = trio.lowlevel.current_task()
    task_ctx: Context = task.context
    assert _ctxvar_MsgCodec not in task_ctx

    nsp_codec: MsgCodec = mk_custom_codec()
    with apply_codec(nsp_codec) as codec:
        chk_codec_applied(
            custom_codec=nsp_codec,
            enter_value=codec,
        )

        nsp = NamespacePath.from_ref(ex_func)
        await ctx.started(nsp)

        async with ctx.open_stream() as ipc:
            async for msg in ipc:

                assert msg == f'{__name__}:ex_func'

                # TODO: as per below
                # assert isinstance(msg, NamespacePath)
                assert isinstance(msg, str)


def chk_codec_applied(
    custom_codec: MsgCodec,
    enter_value: MsgCodec,
) -> MsgCodec:

    task: trio.Task = trio.lowlevel.current_task()
    task_ctx: Context = task.context

    assert _ctxvar_MsgCodec in task_ctx
    curr_codec: MsgCodec = task.context[_ctxvar_MsgCodec]

    assert (
        # returned from `mk_codec()`
        custom_codec is

        # yielded value from `apply_codec()`
        enter_value is

        # read from current task's `contextvars.Context`
        curr_codec is

        # public API for all of the above
        current_msgspec_codec()

        # the default `msgspec` settings
        is not _def_msgspec_codec
    )


def test_codec_hooks_mod():
    '''
    Audit the `.msg.MsgCodec` override apis details given our impl
    uses `contextvars` to accomplish per `trio` task codec
    application around an inter-proc-task-comms context.

    '''
    async def main():
        task: trio.Task = trio.lowlevel.current_task()
        task_ctx: Context = task.context
        assert _ctxvar_MsgCodec not in task_ctx

        async with tractor.open_nursery() as an:
            p: tractor.Portal = await an.start_actor(
                'sub',
                enable_modules=[__name__],
            )

            # TODO: 2 cases:
            # - codec not modified -> decode nsp as `str`
            # - codec modified with hooks -> decode nsp as
            #   `NamespacePath`
            nsp_codec: MsgCodec = mk_custom_codec()
            with apply_codec(nsp_codec) as codec:
                chk_codec_applied(
                    custom_codec=nsp_codec,
                    enter_value=codec,
                )

                async with (
                    p.open_context(
                        send_back_nsp,
                    ) as (ctx, first),
                    ctx.open_stream() as ipc,
                ):
                    # ensure codec is still applied across
                    # `tractor.Context` + its embedded nursery.
                    chk_codec_applied(
                        custom_codec=nsp_codec,
                        enter_value=codec,
                    )

                    assert first == f'{__name__}:ex_func'
                    # TODO: actually get the decoder loading
                    # to native once we spec our SCIPP msgspec
                    # (structurred-conc-inter-proc-protocol)
                    # implemented as per,
                    # https://github.com/goodboy/tractor/issues/36
                    #
                    # assert isinstance(first, NamespacePath)
                    assert isinstance(first, str)
                    await ipc.send(first)

                    with trio.move_on_after(1):
                        async for msg in ipc:

                            # TODO: as per above
                            # assert isinstance(msg, NamespacePath)
                            assert isinstance(msg, str)

            await p.cancel_actor()

    trio.run(main)


def chk_pld_type(
    generic: Msg|_GenericAlias,
    payload_type: Type[Struct]|Any,
    pld: Any,

) -> bool:

    roundtrip: bool = False
    pld_val_type: Type = type(pld)

    # gen_paramed: _GenericAlias = generic[payload_type]
    # for typedef in (
    #     [gen_paramed]
    #     +
    #     # type-var should always be set for these sub-types
    #     # as well!
    #     Msg.__subclasses__()
    # ):
    #     if typedef.__name__ not in [
    #         'Msg',
    #         'Started',
    #         'Yield',
    #         'Return',
    #     ]:
    #         continue

    # TODO: verify that the overridden subtypes
    # DO NOT have modified type-annots from original!
    # 'Start',  .pld: FuncSpec
    # 'StartAck',  .pld: IpcCtxSpec
    # 'Stop',  .pld: UNSEt
    # 'Error',  .pld: ErrorData


    pld_type_spec: Union[Type[Struct]]
    msg_types: list[Msg[payload_type]]

    # make a one-off dec to compare with our `MsgCodec` instance
    # which does the below `mk_msg_spec()` call internally
    (
        pld_type_spec,
        msg_types,
    ) = mk_msg_spec(
        payload_type_union=payload_type,
    )
    enc = msgpack.Encoder()
    dec = msgpack.Decoder(
        type=pld_type_spec or Any,  # like `Msg[Any]`
    )

    codec: MsgCodec = mk_codec(
        # NOTE: this ONLY accepts `Msg.pld` fields of a specified
        # type union.
        ipc_pld_spec=payload_type,
    )

    # assert codec.dec == dec
    # XXX-^ not sure why these aren't "equal" but when cast
    # to `str` they seem to match ?? .. kk
    assert (
        str(pld_type_spec)
        ==
        str(codec.ipc_pld_spec)
        ==
        str(dec.type)
        ==
        str(codec.dec.type)
    )

    # verify the boxed-type for all variable payload-type msgs.
    for typedef in msg_types:

        pld_field = structs.fields(typedef)[1]
        assert pld_field.type is payload_type
        # TODO-^ does this need to work to get all subtypes to adhere?

        kwargs: dict[str, Any] = {
            'cid': '666',
            'pld': pld,
        }
        enc_msg: Msg = typedef(**kwargs)

        wire_bytes: bytes = codec.enc.encode(enc_msg)
        _wire_bytes: bytes = enc.encode(enc_msg)

        try:
            _dec_msg = dec.decode(wire_bytes)
            dec_msg = codec.dec.decode(wire_bytes)

            assert dec_msg.pld == pld
            assert _dec_msg.pld == pld
            assert (roundtrip := (_dec_msg == enc_msg))

        except ValidationError as ve:
            if pld_val_type is payload_type:
                raise ValueError(
                   'Got `ValidationError` despite type-var match!?\n'
                    f'pld_val_type: {pld_val_type}\n'
                    f'payload_type: {payload_type}\n'
                ) from ve

            else:
                # ow we good cuz the pld spec mismatched.
                print(
                    'Got expected `ValidationError` since,\n'
                    f'{pld_val_type} is not {payload_type}\n'
                )
        else:
            if (
                pld_val_type is not payload_type
                and payload_type is not Any
            ):
                raise ValueError(
                   'DID NOT `ValidationError` despite expected type match!?\n'
                    f'pld_val_type: {pld_val_type}\n'
                    f'payload_type: {payload_type}\n'
                )

    return roundtrip



def test_limit_msgspec():

    async def main():
        async with tractor.open_root_actor(
            debug_mode=True
        ):

            # ensure we can round-trip a boxing `Msg`
            assert chk_pld_type(
                Msg,
                Any,
                None,
            )

            # TODO: don't need this any more right since
            # `msgspec>=0.15` has the nice generics stuff yah??
            #
            # manually override the type annot of the payload
            # field and ensure it propagates to all msg-subtypes.
            # Msg.__annotations__['pld'] = Any

            # verify that a mis-typed payload value won't decode
            assert not chk_pld_type(
                Msg,
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
                Msg,
                CustomPayload,
                pld='doggy',
            )

            assert chk_pld_type(
                Msg,
                CustomPayload,
                pld=CustomPayload(name='doggy', value='urmom')
            )

            # uhh bc we can `.pause_from_sync()` now! :surfer:
            # breakpoint()

    trio.run(main)
