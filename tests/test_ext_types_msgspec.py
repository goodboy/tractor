'''
Low-level functional audits for our
"capability based messaging"-spec feats.

B~)

'''
from __future__ import annotations
from contextlib import (
    contextmanager as cm,
    # nullcontext,
)
import importlib
from typing import (
    Any,
    Type,
    Union,
)

from msgspec import (
    # structs,
    # msgpack,
    Raw,
    Struct,
    ValidationError,
)
import pytest
import trio

import tractor
from tractor import (
    Actor,
    # _state,
    MsgTypeError,
    Context,
)
from tractor.msg import (
    _codec,
    _ctxvar_MsgCodec,
    _exts,

    NamespacePath,
    MsgCodec,
    MsgDec,
    mk_codec,
    mk_dec,
    apply_codec,
    current_codec,
)
from tractor.msg._codec import (
    default_builtins,
    mk_dec_hook,
    mk_codec_from_spec,
)
from tractor.msg.types import (
    log,
    Started,
    # _payload_msgs,
    # PayloadMsg,
    # mk_msg_spec,
)
from tractor.msg._ops import (
    limit_plds,
)

def enc_nsp(obj: Any) -> Any:
    actor: Actor = tractor.current_actor(
        err_on_no_runtime=False,
    )
    uid: tuple[str, str]|None = None if not actor else actor.uid
    print(f'{uid} ENC HOOK')

    match obj:
        # case NamespacePath()|str():
        case NamespacePath():
            encoded: str = str(obj)
            print(
                f'----- ENCODING `NamespacePath` as `str` ------\n'
                f'|_obj:{type(obj)!r} = {obj!r}\n'
                f'|_encoded: str = {encoded!r}\n'
            )
            # if type(obj) != NamespacePath:
            #     breakpoint()
            return encoded
        case _:
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
    # breakpoint()
    actor: Actor = tractor.current_actor(
        err_on_no_runtime=False,
    )
    uid: tuple[str, str]|None = None if not actor else actor.uid
    print(
        f'{uid}\n'
        'CUSTOM DECODE\n'
        f'type-arg-> {obj_type}\n'
        f'obj-arg-> `{obj}`: {type(obj)}\n'
    )
    nsp = None
    # XXX, never happens right?
    if obj_type is Raw:
        breakpoint()

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


def ex_func(*args):
    '''
    A mod level func we can ref and load via our `NamespacePath`
    python-object pointer `str` subtype.

    '''
    print(f'ex_func({args})')


@pytest.mark.parametrize(
    'add_codec_hooks',
    [
        True,
        False,
    ],
    ids=['use_codec_hooks', 'no_codec_hooks'],
)
def test_custom_extension_types(
    debug_mode: bool,
    add_codec_hooks: bool
):
    '''
    Verify that a `MsgCodec` (used for encoding all outbound IPC msgs
    and decoding all inbound `PayloadMsg`s) and a paired `MsgDec`
    (used for decoding the `PayloadMsg.pld: Raw` received within a given
    task's ipc `Context` scope) can both send and receive "extension types"
    as supported via custom converter hooks passed to `msgspec`.

    '''
    nsp_pld_dec: MsgDec = mk_dec(
        spec=None,  # ONLY support the ext type
        dec_hook=dec_nsp if add_codec_hooks else None,
        ext_types=[NamespacePath],
    )
    nsp_codec: MsgCodec = mk_codec(
        # ipc_pld_spec=Raw,  # default!

        # NOTE XXX: the encode hook MUST be used no matter what since
        # our `NamespacePath` is not any of a `Any` native type nor
        # a `msgspec.Struct` subtype - so `msgspec` has no way to know
        # how to encode it unless we provide the custom hook.
        #
        # AGAIN that is, regardless of whether we spec an
        # `Any`-decoded-pld the enc has no knowledge (by default)
        # how to enc `NamespacePath` (nsp), so we add a custom
        # hook to do that ALWAYS.
        enc_hook=enc_nsp if add_codec_hooks else None,

        # XXX NOTE: pretty sure this is mutex with the `type=` to
        # `Decoder`? so it won't work in tandem with the
        # `ipc_pld_spec` passed above?
        ext_types=[NamespacePath],

        # TODO? is it useful to have the `.pld` decoded *prior* to
        # the `PldRx`?? like perf or mem related?
        # ext_dec=nsp_pld_dec,
    )
    if add_codec_hooks:
        assert nsp_codec.dec.dec_hook is None

        # TODO? if we pass `ext_dec` above?
        # assert nsp_codec.dec.dec_hook is dec_nsp

        assert nsp_codec.enc.enc_hook is enc_nsp

    nsp = NamespacePath.from_ref(ex_func)

    try:
        nsp_bytes: bytes = nsp_codec.encode(nsp)
        nsp_rt_sin_msg = nsp_pld_dec.decode(nsp_bytes)
        nsp_rt_sin_msg.load_ref() is ex_func
    except TypeError:
        if not add_codec_hooks:
            pass

    try:
        msg_bytes: bytes = nsp_codec.encode(
            Started(
                cid='cid',
                pld=nsp,
            )
        )
        # since the ext-type obj should also be set as the msg.pld
        assert nsp_bytes in msg_bytes
        started_rt: Started = nsp_codec.decode(msg_bytes)
        pld: Raw = started_rt.pld
        assert isinstance(pld, Raw)
        nsp_rt: NamespacePath = nsp_pld_dec.decode(pld)
        assert isinstance(nsp_rt, NamespacePath)
        # in obj comparison terms they should be the same
        assert nsp_rt == nsp
        # ensure we've decoded to ext type!
        assert nsp_rt.load_ref() is ex_func

    except TypeError:
        if not add_codec_hooks:
            pass

@tractor.context
async def sleep_forever_in_sub(
    ctx: Context,
) -> None:
    await trio.sleep_forever()


def mk_custom_codec(
    add_hooks: bool,

) -> tuple[
    MsgCodec,  # encode to send
    MsgDec,  # pld receive-n-decode
]:
    '''
    Create custom `msgpack` enc/dec-hooks and set a `Decoder`
    which only loads `pld_spec` (like `NamespacePath`) types.

    '''

    # XXX NOTE XXX: despite defining `NamespacePath` as a type
    # field on our `PayloadMsg.pld`, we still need a enc/dec_hook() pair
    # to cast to/from that type on the wire. See the docs:
    # https://jcristharif.com/msgspec/extending.html#mapping-to-from-native-types

    # if pld_spec is Any:
    #     pld_spec = Raw

    nsp_codec: MsgCodec = mk_codec(
        # ipc_pld_spec=Raw,  # default!

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
        ext_types=[NamespacePath],
    )
    # dec_hook=dec_nsp if add_hooks else None,
    return nsp_codec


@pytest.mark.parametrize(
    'limit_plds_args',
    [
        (
            {'dec_hook': None, 'ext_types': None},
            None,
        ),
        (
            {'dec_hook': dec_nsp, 'ext_types': None},
            TypeError,
        ),
        (
            {'dec_hook': dec_nsp, 'ext_types': [NamespacePath]},
            None,
        ),
        (
            {'dec_hook': dec_nsp, 'ext_types': [NamespacePath|None]},
            None,
        ),
    ],
    ids=[
        'no_hook_no_ext_types',
        'only_hook',
        'hook_and_ext_types',
        'hook_and_ext_types_w_null',
    ]
)
def test_pld_limiting_usage(
    limit_plds_args: tuple[dict, Exception|None],
):
    '''
    Verify `dec_hook()` and `ext_types` need to either both be
    provided or we raise a explanator type-error.

    '''
    kwargs, maybe_err = limit_plds_args
    async def main():
        async with tractor.open_nursery() as an:  # just to open runtime

            # XXX SHOULD NEVER WORK outside an ipc ctx scope!
            try:
                with limit_plds(**kwargs):
                    pass
            except RuntimeError:
                pass

            p: tractor.Portal = await an.start_actor(
                'sub',
                enable_modules=[__name__],
            )
            async with (
                p.open_context(
                    sleep_forever_in_sub
                ) as (ctx, first),
            ):
                try:
                    with limit_plds(**kwargs):
                        pass
                except maybe_err as exc:
                    assert type(exc) is maybe_err
                    pass


def chk_codec_applied(
    expect_codec: MsgCodec|None,
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
    if expect_codec is None:
        assert enter_value is None
        return

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
        assert enter_value is same_codec


@tractor.context
async def send_back_values(
    ctx: Context,
    rent_pld_spec_type_strs: list[str],
    add_hooks: bool,

) -> None:
    '''
    Setup up a custom codec to load instances of `NamespacePath`
    and ensure we can round trip a func ref with our parent.

    '''
    uid: tuple = tractor.current_actor().uid

    # init state in sub-actor should be default
    chk_codec_applied(
        expect_codec=_codec._def_tractor_codec,
    )

    # load pld spec from input str
    rent_pld_spec = _exts.dec_type_union(
        rent_pld_spec_type_strs,
        mods=[
            importlib.import_module(__name__),
        ],
    )
    rent_pld_spec_types: set[Type] = _codec.unpack_spec_types(
        rent_pld_spec,
    )

    # ONLY add ext-hooks if the rent specified a non-std type!
    add_hooks: bool = (
        NamespacePath in rent_pld_spec_types
        and
        add_hooks
    )

    # same as on parent side config.
    nsp_codec: MsgCodec|None = None
    if add_hooks:
        nsp_codec = mk_codec(
            enc_hook=enc_nsp,
            ext_types=[NamespacePath],
        )

    with (
        maybe_apply_codec(nsp_codec) as codec,
        limit_plds(
            rent_pld_spec,
            dec_hook=dec_nsp if add_hooks else None,
            ext_types=[NamespacePath]  if add_hooks else None,
        ) as pld_dec,
    ):
        # ?XXX? SHOULD WE NOT be swapping the global codec since it
        # breaks `Context.started()` roundtripping checks??
        chk_codec_applied(
            expect_codec=nsp_codec,
            enter_value=codec,
        )

        # ?TODO, mismatch case(s)?
        #
        # ensure pld spec matches on both sides
        ctx_pld_dec: MsgDec = ctx._pld_rx._pld_dec
        assert pld_dec is ctx_pld_dec
        child_pld_spec: Type = pld_dec.spec
        child_pld_spec_types: set[Type] = _codec.unpack_spec_types(
            child_pld_spec,
        )
        assert (
            child_pld_spec_types.issuperset(
                rent_pld_spec_types
            )
        )

        # ?TODO, try loop for each of the types in pld-superset?
        #
        # for send_value in [
        #     nsp,
        #     str(nsp),
        #     None,
        # ]:
        nsp = NamespacePath.from_ref(ex_func)
        try:
            print(
                f'{uid}: attempting to `.started({nsp})`\n'
                f'\n'
                f'rent_pld_spec: {rent_pld_spec}\n'
                f'child_pld_spec: {child_pld_spec}\n'
                f'codec: {codec}\n'
            )
            # await tractor.pause()
            await ctx.started(nsp)

        except tractor.MsgTypeError as _mte:
            mte = _mte

            # false -ve case
            if add_hooks:
                raise RuntimeError(
                    f'EXPECTED to `.started()` value given spec ??\n\n'
                    f'child_pld_spec -> {child_pld_spec}\n'
                    f'value = {nsp}: {type(nsp)}\n'
                )

            # true -ve case
            raise mte

        # TODO: maybe we should add our own wrapper error so as to
        # be interchange-lib agnostic?
        # -[ ] the error type is wtv is raised from the hook so we
        #   could also require a type-class of errors for
        #   indicating whether the hook-failure can be handled by
        #   a nasty-dialog-unprot sub-sys?
        except TypeError as typerr:
            # false -ve
            if add_hooks:
                raise RuntimeError('Should have been able to send `nsp`??')

            # true -ve
            print('Failed to send `nsp` due to no ext hooks set!')
            raise typerr

        # now try sending a set of valid and invalid plds to ensure
        # the pld spec is respected.
        sent: list[Any] = []
        async with ctx.open_stream() as ipc:
            print(
                f'{uid}: streaming all pld types to rent..'
            )

            # for send_value, expect_send in iter_send_val_items:
            for send_value in [
                nsp,
                str(nsp),
                None,
            ]:
                send_type: Type = type(send_value)
                print(
                    f'{uid}: SENDING NEXT pld\n'
                    f'send_type: {send_type}\n'
                    f'send_value: {send_value}\n'
                )
                try:
                    await ipc.send(send_value)
                    sent.append(send_value)

                except ValidationError as valerr:
                    print(f'{uid} FAILED TO SEND {send_value}!')

                    # false -ve
                    if add_hooks:
                        raise RuntimeError(
                            f'EXPECTED to roundtrip value given spec:\n'
                            f'rent_pld_spec -> {rent_pld_spec}\n'
                            f'child_pld_spec -> {child_pld_spec}\n'
                            f'value = {send_value}: {send_type}\n'
                        )

                    # true -ve
                    raise valerr
                    # continue

            else:
                print(
                    f'{uid}: finished sending all values\n'
                    'Should be exiting stream block!\n'
                )

        print(f'{uid}: exited streaming block!')



@cm
def maybe_apply_codec(codec: MsgCodec|None) -> MsgCodec|None:
    if codec is None:
        yield None
        return

    with apply_codec(codec) as codec:
        yield codec


@pytest.mark.parametrize(
    'pld_spec',
    [
        Any,
        NamespacePath,
        NamespacePath|None,  # the "maybe" spec Bo
    ],
    ids=[
        'any_type',
        'only_nsp_ext',
        'maybe_nsp_ext',
    ]
)
@pytest.mark.parametrize(
    'add_hooks',
    [
        True,
        False,
    ],
    ids=[
        'use_codec_hooks',
        'no_codec_hooks',
    ],
)
def test_ext_types_over_ipc(
    debug_mode: bool,
    pld_spec: Union[Type],
    add_hooks: bool,
):
    '''
    Ensure we can support extension types coverted using
    `enc/dec_hook()`s passed to the `.msg.limit_plds()` API
    and that sane errors happen when we try do the same without
    the codec hooks.

    '''
    pld_types: set[Type] = _codec.unpack_spec_types(pld_spec)

    async def main():

        # sanity check the default pld-spec beforehand
        chk_codec_applied(
            expect_codec=_codec._def_tractor_codec,
        )

        # extension type we want to send as msg payload
        nsp = NamespacePath.from_ref(ex_func)

        # ^NOTE, 2 cases:
        # - codec hooks noto added -> decode nsp as `str`
        # - codec with hooks -> decode nsp as `NamespacePath`
        nsp_codec: MsgCodec|None = None
        if (
            NamespacePath in pld_types
            and
            add_hooks
        ):
            nsp_codec = mk_codec(
                enc_hook=enc_nsp,
                ext_types=[NamespacePath],
            )

        async with tractor.open_nursery(
            debug_mode=debug_mode,
        ) as an:
            p: tractor.Portal = await an.start_actor(
                'sub',
                enable_modules=[__name__],
            )
            with (
                maybe_apply_codec(nsp_codec) as codec,
            ):
                chk_codec_applied(
                    expect_codec=nsp_codec,
                    enter_value=codec,
                )
                rent_pld_spec_type_strs: list[str] = _exts.enc_type_union(pld_spec)

                # XXX should raise an mte (`MsgTypeError`)
                # when `add_hooks == False` bc the input
                # `expect_ipc_send` kwarg has a nsp which can't be
                # serialized!
                #
                # TODO:can we ensure this happens from the
                # `Return`-side (aka the sub) as well?
                try:
                    ctx: tractor.Context
                    ipc: tractor.MsgStream
                    async with (

                        # XXX should raise an mte (`MsgTypeError`)
                        # when `add_hooks == False`..
                        p.open_context(
                            send_back_values,
                            # expect_debug=debug_mode,
                            rent_pld_spec_type_strs=rent_pld_spec_type_strs,
                            add_hooks=add_hooks,
                            # expect_ipc_send=expect_ipc_send,
                        ) as (ctx, first),

                        ctx.open_stream() as ipc,
                    ):
                        with (
                            limit_plds(
                                pld_spec,
                                dec_hook=dec_nsp if add_hooks else None,
                                ext_types=[NamespacePath]  if add_hooks else None,
                            ) as pld_dec,
                        ):
                            ctx_pld_dec: MsgDec = ctx._pld_rx._pld_dec
                            assert pld_dec is ctx_pld_dec

                            # if (
                            #     not add_hooks
                            #     and
                            #     NamespacePath in 
                            # ):
                            #     pytest.fail('ctx should fail to open without custom enc_hook!?')

                            await ipc.send(nsp)
                            nsp_rt = await ipc.receive()

                            assert nsp_rt == nsp
                            assert nsp_rt.load_ref() is ex_func

                # this test passes bc we can go no further!
                except MsgTypeError as mte:
                    # if not add_hooks:
                    #     # teardown nursery
                    #     await p.cancel_actor()
                        # return

                    raise mte

            await p.cancel_actor()

    if (
        NamespacePath in pld_types
        and
        add_hooks
    ):
        trio.run(main)

    else:
        with pytest.raises(
            expected_exception=tractor.RemoteActorError,
        ) as excinfo:
            trio.run(main)

        exc = excinfo.value
        # bc `.started(nsp: NamespacePath)` will raise
        assert exc.boxed_type is TypeError


'''
Test the auto enc & dec hooks

Create a codec which will work for:
    - builtins
    - custom types
    - lists of custom types
'''


class TestBytesClass(Struct, tag=True):
    raw: bytes

    def encode(self) -> bytes:
        return self.raw

    @classmethod
    def from_bytes(self, raw: bytes) -> TestBytesClass:
        return TestBytesClass(raw=raw)


class TestStrClass(Struct, tag=True):
    s: str

    def encode(self) -> str:
        return self.s

    @classmethod
    def from_str(self, s: str) -> TestStrClass:
        return TestStrClass(s=s)


class TestIntClass(Struct, tag=True):
    num: int

    def encode(self) -> int:
        return self.num

    @classmethod
    def from_int(self, num: int) -> TestIntClass:
        return TestIntClass(num=num)


builtins = tuple((
    builtin
    for builtin in default_builtins
    if builtin is not list
))

TestClasses = (TestBytesClass, TestStrClass, TestIntClass)


TestSpec = (
    *TestClasses, list[Union[*TestClasses]]
)


test_codec = mk_codec_from_spec(
    spec=TestSpec
)


@tractor.context
async def child_custom_codec(
    ctx: tractor.Context,
    msgs: list[Union[*TestSpec]],
):
    '''
    Apply codec and send all msgs passed through stream

    '''
    with (
        apply_codec(test_codec),
        limit_plds(
            test_codec.pld_spec,
            dec_hook=mk_dec_hook(TestSpec),
            ext_types=TestSpec + builtins
        ),
    ):
        await ctx.started(None)
        async with ctx.open_stream() as stream:
            for msg in msgs:
                await stream.send(msg)


def test_multi_custom_codec():
    '''
    Open subactor setup codec and pld_rx and wait to receive & assert from
    stream

    '''
    msgs = [
        None,
        True, False,
        0xdeadbeef,
        .42069,
        b'deadbeef',
        TestBytesClass(raw=b'deadbeef'),
        TestStrClass(s='deadbeef'),
        TestIntClass(num=0xdeadbeef),
        [
            TestBytesClass(raw=b'deadbeef'),
            TestStrClass(s='deadbeef'),
            TestIntClass(num=0xdeadbeef),
        ]
    ]

    async def main():
        async with tractor.open_nursery() as an:
            p: tractor.Portal = await an.start_actor(
                'child',
                enable_modules=[__name__],
            )
            async with (
                p.open_context(
                    child_custom_codec,
                    msgs=msgs,
                ) as (ctx, _),
                ctx.open_stream() as ipc
            ):
                with (
                    apply_codec(test_codec),
                    limit_plds(
                        test_codec.pld_spec,
                        dec_hook=mk_dec_hook(TestSpec),
                        ext_types=TestSpec + builtins
                    )
                ):
                    msg_iter = iter(msgs)
                    async for recv_msg in ipc:
                        assert recv_msg == next(msg_iter)

            await p.cancel_actor()

    trio.run(main)


# def chk_pld_type(
#     payload_spec: Type[Struct]|Any,
#     pld: Any,

#     expect_roundtrip: bool|None = None,

# ) -> bool:

#     pld_val_type: Type = type(pld)

#     # TODO: verify that the overridden subtypes
#     # DO NOT have modified type-annots from original!
#     # 'Start',  .pld: FuncSpec
#     # 'StartAck',  .pld: IpcCtxSpec
#     # 'Stop',  .pld: UNSEt
#     # 'Error',  .pld: ErrorData

#     codec: MsgCodec = mk_codec(
#         # NOTE: this ONLY accepts `PayloadMsg.pld` fields of a specified
#         # type union.
#         ipc_pld_spec=payload_spec,
#     )

#     # make a one-off dec to compare with our `MsgCodec` instance
#     # which does the below `mk_msg_spec()` call internally
#     ipc_msg_spec: Union[Type[Struct]]
#     msg_types: list[PayloadMsg[payload_spec]]
#     (
#         ipc_msg_spec,
#         msg_types,
#     ) = mk_msg_spec(
#         payload_type_union=payload_spec,
#     )
#     _enc = msgpack.Encoder()
#     _dec = msgpack.Decoder(
#         type=ipc_msg_spec or Any,  # like `PayloadMsg[Any]`
#     )

#     assert (
#         payload_spec
#         ==
#         codec.pld_spec
#     )

#     # assert codec.dec == dec
#     #
#     # ^-XXX-^ not sure why these aren't "equal" but when cast
#     # to `str` they seem to match ?? .. kk

#     assert (
#         str(ipc_msg_spec)
#         ==
#         str(codec.msg_spec)
#         ==
#         str(_dec.type)
#         ==
#         str(codec.dec.type)
#     )

#     # verify the boxed-type for all variable payload-type msgs.
#     if not msg_types:
#         breakpoint()

#     roundtrip: bool|None = None
#     pld_spec_msg_names: list[str] = [
#         td.__name__ for td in _payload_msgs
#     ]
#     for typedef in msg_types:

#         skip_runtime_msg: bool = typedef.__name__ not in pld_spec_msg_names
#         if skip_runtime_msg:
#             continue

#         pld_field = structs.fields(typedef)[1]
#         assert pld_field.type is payload_spec # TODO-^ does this need to work to get all subtypes to adhere?

#         kwargs: dict[str, Any] = {
#             'cid': '666',
#             'pld': pld,
#         }
#         enc_msg: PayloadMsg = typedef(**kwargs)

#         _wire_bytes: bytes = _enc.encode(enc_msg)
#         wire_bytes: bytes = codec.enc.encode(enc_msg)
#         assert _wire_bytes == wire_bytes

#         ve: ValidationError|None = None
#         try:
#             dec_msg = codec.dec.decode(wire_bytes)
#             _dec_msg = _dec.decode(wire_bytes)

#             # decoded msg and thus payload should be exactly same!
#             assert (roundtrip := (
#                 _dec_msg
#                 ==
#                 dec_msg
#                 ==
#                 enc_msg
#             ))

#             if (
#                 expect_roundtrip is not None
#                 and expect_roundtrip != roundtrip
#             ):
#                 breakpoint()

#             assert (
#                 pld
#                 ==
#                 dec_msg.pld
#                 ==
#                 enc_msg.pld
#             )
#             # assert (roundtrip := (_dec_msg == enc_msg))

#         except ValidationError as _ve:
#             ve = _ve
#             roundtrip: bool = False
#             if pld_val_type is payload_spec:
#                 raise ValueError(
#                    'Got `ValidationError` despite type-var match!?\n'
#                     f'pld_val_type: {pld_val_type}\n'
#                     f'payload_type: {payload_spec}\n'
#                 ) from ve

#             else:
#                 # ow we good cuz the pld spec mismatched.
#                 print(
#                     'Got expected `ValidationError` since,\n'
#                     f'{pld_val_type} is not {payload_spec}\n'
#                 )
#         else:
#             if (
#                 payload_spec is not Any
#                 and
#                 pld_val_type is not payload_spec
#             ):
#                 raise ValueError(
#                    'DID NOT `ValidationError` despite expected type match!?\n'
#                     f'pld_val_type: {pld_val_type}\n'
#                     f'payload_type: {payload_spec}\n'
#                 )

#     # full code decode should always be attempted!
#     if roundtrip is None:
#         breakpoint()

#     return roundtrip


# ?TODO? maybe remove since covered in the newer `test_pldrx_limiting`
# via end-2-end testing of all this?
# -[ ] IOW do we really NEED this lowlevel unit testing?
#
# def test_limit_msgspec(
#     debug_mode: bool,
# ):
#     '''
#     Internals unit testing to verify that type-limiting an IPC ctx's
#     msg spec with `Pldrx.limit_plds()` results in various
#     encapsulated `msgspec` object settings and state.

#     '''
#     async def main():
#         async with tractor.open_root_actor(
#             debug_mode=debug_mode,
#         ):
#             # ensure we can round-trip a boxing `PayloadMsg`
#             assert chk_pld_type(
#                 payload_spec=Any,
#                 pld=None,
#                 expect_roundtrip=True,
#             )

#             # verify that a mis-typed payload value won't decode
#             assert not chk_pld_type(
#                 payload_spec=int,
#                 pld='doggy',
#             )

#             # parametrize the boxed `.pld` type as a custom-struct
#             # and ensure that parametrization propagates
#             # to all payload-msg-spec-able subtypes!
#             class CustomPayload(Struct):
#                 name: str
#                 value: Any

#             assert not chk_pld_type(
#                 payload_spec=CustomPayload,
#                 pld='doggy',
#             )

#             assert chk_pld_type(
#                 payload_spec=CustomPayload,
#                 pld=CustomPayload(name='doggy', value='urmom')
#             )

#             # yah, we can `.pause_from_sync()` now!
#             # breakpoint()

#     trio.run(main)
