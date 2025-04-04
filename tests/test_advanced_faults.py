'''
Sketchy network blackoutz, ugly byzantine gens, puedes eschuchar la
cancelacion?..

'''
from functools import partial
from types import ModuleType

import pytest
from _pytest.pathlib import import_path
import trio
import tractor
from tractor._testing import (
    examples_dir,
    break_ipc,
)


@pytest.mark.parametrize(
    'pre_aclose_msgstream',
    [
        False,
        True,
    ],
    ids=[
        'no_msgstream_aclose',
        'pre_aclose_msgstream',
    ],
)
@pytest.mark.parametrize(
    'ipc_break',
    [
        # no breaks
        {
            'break_parent_ipc_after': False,
            'break_child_ipc_after': False,
        },

        # only parent breaks
        {
            'break_parent_ipc_after': 500,
            'break_child_ipc_after': False,
        },

        # only child breaks
        {
            'break_parent_ipc_after': False,
            'break_child_ipc_after': 500,
        },

        # both: break parent first
        {
            'break_parent_ipc_after': 500,
            'break_child_ipc_after': 800,
        },
        # both: break child first
        {
            'break_parent_ipc_after': 800,
            'break_child_ipc_after': 500,
        },

    ],
    ids=[
        'no_break',
        'break_parent',
        'break_child',
        'break_both_parent_first',
        'break_both_child_first',
    ],
)
def test_ipc_channel_break_during_stream(
    debug_mode: bool,
    loglevel: str,
    spawn_backend: str,
    ipc_break: dict|None,
    pre_aclose_msgstream: bool,
    tpt_proto: str,
):
    '''
    Ensure we can have an IPC channel break its connection during
    streaming and it's still possible for the (simulated) user to kill
    the actor tree using SIGINT.

    We also verify the type of connection error expected in the parent
    depending on which side if the IPC breaks first.

    '''
    if spawn_backend != 'trio':
        if debug_mode:
            pytest.skip('`debug_mode` only supported on `trio` spawner')

        # non-`trio` spawners should never hit the hang condition that
        # requires the user to do ctl-c to cancel the actor tree.
        # expect_final_exc = trio.ClosedResourceError
        expect_final_exc = tractor.TransportClosed

    mod: ModuleType = import_path(
        examples_dir() / 'advanced_faults'
        / 'ipc_failure_during_stream.py',
        root=examples_dir(),
        consider_namespace_packages=False,
    )

    # by def we expect KBI from user after a simulated "hang
    # period" wherein the user eventually hits ctl-c to kill the
    # root-actor tree.
    expect_final_exc: BaseException = KeyboardInterrupt
    if (
        # only expect EoC if trans is broken on the child side,
        ipc_break['break_child_ipc_after'] is not False
        # AND we tell the child to call `MsgStream.aclose()`.
        and pre_aclose_msgstream
    ):
        # expect_final_exc = trio.EndOfChannel
        # ^XXX NOPE! XXX^ since now `.open_stream()` absorbs this
        # gracefully!
        expect_final_exc = KeyboardInterrupt

    # NOTE when ONLY the child breaks or it breaks BEFORE the
    # parent we expect the parent to get a closed resource error
    # on the next `MsgStream.receive()` and then fail out and
    # cancel the child from there.
    #
    # ONLY CHILD breaks
    if (
        ipc_break['break_child_ipc_after']
        and
        ipc_break['break_parent_ipc_after'] is False
    ):
        # NOTE: we DO NOT expect this any more since
        # the child side's channel will be broken silently
        # and nothing on the parent side will indicate this!
        # expect_final_exc = trio.ClosedResourceError

        # NOTE: child will send a 'stop' msg before it breaks
        # the transport channel BUT, that will be absorbed by the
        # `ctx.open_stream()` block and thus the `.open_context()`
        # should hang, after which the test script simulates
        # a user sending ctl-c by raising a KBI.
        if pre_aclose_msgstream:
            expect_final_exc = KeyboardInterrupt

            # XXX OLD XXX
            # if child calls `MsgStream.aclose()` then expect EoC.
            # ^ XXX not any more ^ since eoc is always absorbed
            # gracefully and NOT bubbled to the `.open_context()`
            # block!
            # expect_final_exc = trio.EndOfChannel

    # BOTH but, CHILD breaks FIRST
    elif (
        ipc_break['break_child_ipc_after'] is not False
        and (
            ipc_break['break_parent_ipc_after']
            > ipc_break['break_child_ipc_after']
        )
    ):
        if pre_aclose_msgstream:
            expect_final_exc = KeyboardInterrupt

    # NOTE when the parent IPC side dies (even if the child does as well
    # but the child fails BEFORE the parent) we always expect the
    # IPC layer to raise a closed-resource, NEVER do we expect
    # a stop msg since the parent-side ctx apis will error out
    # IMMEDIATELY before the child ever sends any 'stop' msg.
    #
    # ONLY PARENT breaks
    elif (
        ipc_break['break_parent_ipc_after']
        and
        ipc_break['break_child_ipc_after'] is False
    ):
        # expect_final_exc = trio.ClosedResourceError
        expect_final_exc = tractor.TransportClosed

    # BOTH but, PARENT breaks FIRST
    elif (
        ipc_break['break_parent_ipc_after'] is not False
        and (
            ipc_break['break_child_ipc_after']
            >
            ipc_break['break_parent_ipc_after']
        )
    ):
        # expect_final_exc = trio.ClosedResourceError
        expect_final_exc = tractor.TransportClosed

    with pytest.raises(
        expected_exception=(
            expect_final_exc,
            ExceptionGroup,
        ),
    ) as excinfo:
        try:
            trio.run(
                partial(
                    mod.main,
                    debug_mode=debug_mode,
                    start_method=spawn_backend,
                    loglevel=loglevel,
                    pre_close=pre_aclose_msgstream,
                    tpt_proto=tpt_proto,
                    **ipc_break,
                )
            )
        except KeyboardInterrupt as _kbi:
            kbi = _kbi
            if expect_final_exc is not KeyboardInterrupt:
                pytest.fail(
                    'Rxed unexpected KBI !?\n'
                    f'{repr(kbi)}'
                )

            raise

        except tractor.TransportClosed as _tc:
            tc = _tc
            if expect_final_exc is KeyboardInterrupt:
                pytest.fail(
                    'Unexpected transport failure !?\n'
                    f'{repr(tc)}'
                )
            cause: Exception = tc.__cause__
            assert (
                type(cause) is trio.ClosedResourceError
                and
                cause.args[0] == 'another task closed this fd'
            )
            raise

    # get raw instance from pytest wrapper
    value = excinfo.value
    if isinstance(value, ExceptionGroup):
        excs = value.exceptions
        assert len(excs) == 1
        final_exc = excs[0]
        assert isinstance(final_exc, expect_final_exc)


@tractor.context
async def break_ipc_after_started(
    ctx: tractor.Context,
) -> None:
    await ctx.started()
    async with ctx.open_stream() as stream:

        # TODO: make a test which verifies the error
        # for this, i.e. raises a `MsgTypeError`
        # await ctx.chan.send(None)

        await break_ipc(
            stream=stream,
            pre_close=True,
        )
        print('child broke IPC and terminating')


def test_stream_closed_right_after_ipc_break_and_zombie_lord_engages():
    '''
    Verify that is a subactor's IPC goes down just after bringing up
    a stream the parent can trigger a SIGINT and the child will be
    reaped out-of-IPC by the localhost process supervision machinery:
    aka "zombie lord".

    '''
    async def main():
        with trio.fail_after(3):
            async with tractor.open_nursery() as an:
                portal = await an.start_actor(
                    'ipc_breaker',
                    enable_modules=[__name__],
                )

                with trio.move_on_after(1):
                    async with (
                        portal.open_context(
                            break_ipc_after_started
                        ) as (ctx, sent),
                    ):
                        async with ctx.open_stream():
                            await trio.sleep(0.5)

                        print('parent waiting on context')

                print(
                    'parent exited context\n'
                    'parent raising KBI..\n'
                )
                raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        trio.run(main)
