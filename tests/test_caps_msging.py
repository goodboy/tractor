'''
Functional audits for our "capability based messaging (schema)" feats.

B~)

'''
from typing import (
    Any,
    Type,
)
from contextvars import (
    Context,
)

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
import trio

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


def mk_custom_codec() -> MsgCodec:
    # apply custom hooks and set a `Decoder` which only
    # loads `NamespacePath` types.
    nsp_codec: MsgCodec = mk_codec(
        dec_types=NamespacePath,
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
