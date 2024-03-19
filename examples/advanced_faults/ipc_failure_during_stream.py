'''
Complex edge case where during real-time streaming the IPC tranport
channels are wiped out (purposely in this example though it could have
been an outage) and we want to ensure that despite being in debug mode
(or not) the user can sent SIGINT once they notice the hang and the
actor tree will eventually be cancelled without leaving any zombies.

'''
from contextlib import asynccontextmanager as acm
from functools import partial

from tractor import (
    open_nursery,
    context,
    Context,
    ContextCancelled,
    MsgStream,
    _testing,
)
import trio
import pytest


async def break_ipc(
    stream: MsgStream,
    method: str|None = None,
    pre_close: bool = False,

    def_method: str = 'eof',

) -> None:
    '''
    XXX: close the channel right after an error is raised
    purposely breaking the IPC transport to make sure the parent
    doesn't get stuck in debug or hang on the connection join.
    this more or less simulates an infinite msg-receive hang on
    the other end.

    '''
    # close channel via IPC prot msging before
    # any transport breakage
    if pre_close:
        await stream.aclose()

    method: str = method or def_method
    print(
        '#################################\n'
        'Simulating CHILD-side IPC BREAK!\n'
        f'method: {method}\n'
        f'pre `.aclose()`: {pre_close}\n'
        '#################################\n'
    )

    match method:
        case 'trans_aclose':
            await stream._ctx.chan.transport.stream.aclose()

        case 'eof':
            await stream._ctx.chan.transport.stream.send_eof()

        case 'msg':
            await stream._ctx.chan.send(None)

        # TODO: the actual real-world simulated cases like
        # transport layer hangs and/or lower layer 2-gens type
        # scenarios..
        #
        # -[ ] already have some issues for this general testing
        # area:
        #  - https://github.com/goodboy/tractor/issues/97
        #  - https://github.com/goodboy/tractor/issues/124
        #   - PR from @guille:
        #     https://github.com/goodboy/tractor/pull/149
        # case 'hang':
        # TODO: framework research:
        #
        # - https://github.com/GuoTengda1993/pynetem
        # - https://github.com/shopify/toxiproxy
        # - https://manpages.ubuntu.com/manpages/trusty/man1/wirefilter.1.html

        case _:
            raise RuntimeError(
                f'IPC break method unsupported: {method}'
            )


async def break_ipc_then_error(
    stream: MsgStream,
    break_ipc_with: str|None = None,
    pre_close: bool = False,
):
    await break_ipc(
        stream=stream,
        method=break_ipc_with,
        pre_close=pre_close,
    )
    async for msg in stream:
        await stream.send(msg)

    assert 0


async def iter_ipc_stream(
    stream: MsgStream,
    break_ipc_with: str|None = None,
    pre_close: bool = False,
):
    async for msg in stream:
        await stream.send(msg)


@context
async def recv_and_spawn_net_killers(

    ctx: Context,
    break_ipc_after: bool|int = False,
    pre_close: bool = False,

) -> None:
    '''
    Receive stream msgs and spawn some IPC killers mid-stream.

    '''
    await ctx.started()
    async with (
        ctx.open_stream() as stream,
        trio.open_nursery() as n,
    ):
        async for i in stream:
            print(f'child echoing {i}')
            await stream.send(i)

            if (
                break_ipc_after
                and
                i >= break_ipc_after
            ):
                n.start_soon(
                    iter_ipc_stream,
                    stream,
                )
                n.start_soon(
                    partial(
                        break_ipc_then_error,
                        stream=stream,
                        pre_close=pre_close,
                    )
                )


@acm
async def stuff_hangin_ctlc(timeout: float = 1) -> None:

    with trio.move_on_after(timeout) as cs:
        yield timeout

    if cs.cancelled_caught:
        # pretend to be a user seeing no streaming action
        # thinking it's a hang, and then hitting ctl-c..
        print(
            f"i'm a user on the PARENT side and thingz hangin "
            f'after timeout={timeout} ???\n\n'
            'MASHING CTlR-C..!?\n'
        )
        raise KeyboardInterrupt


async def main(
    debug_mode: bool = False,
    start_method: str = 'trio',
    loglevel: str = 'cancel',

    # by default we break the parent IPC first (if configured to break
    # at all), but this can be changed so the child does first (even if
    # both are set to break).
    break_parent_ipc_after: int|bool = False,
    break_child_ipc_after: int|bool = False,
    pre_close: bool = False,

) -> None:

    async with (
        open_nursery(
            start_method=start_method,

            # NOTE: even debugger is used we shouldn't get
            # a hang since it never engages due to broken IPC
            debug_mode=debug_mode,
            loglevel=loglevel,

        ) as an,
    ):
        sub_name: str = 'chitty_hijo'
        portal = await an.start_actor(
            sub_name,
            enable_modules=[__name__],
        )

        async with (
            stuff_hangin_ctlc(timeout=2) as timeout,
            _testing.expect_ctxc(
                yay=(
                    break_parent_ipc_after
                    or break_child_ipc_after
                ),
                # TODO: we CAN'T remove this right?
                # since we need the ctxc to bubble up from either
                # the stream API after the `None` msg is sent
                # (which actually implicitly cancels all remote
                # tasks in the hijo) or from simluated
                # KBI-mash-from-user
                # or should we expect that a KBI triggers the ctxc
                # and KBI in an eg?
                reraise=True,
            ),

            portal.open_context(
                recv_and_spawn_net_killers,
                break_ipc_after=break_child_ipc_after,
                pre_close=pre_close,
            ) as (ctx, sent),
        ):
            rx_eoc: bool = False
            ipc_break_sent: bool = False
            async with ctx.open_stream() as stream:
                for i in range(1000):

                    if (
                        break_parent_ipc_after
                        and
                        i > break_parent_ipc_after
                        and
                        not ipc_break_sent
                    ):
                        print(
                            '#################################\n'
                            'Simulating PARENT-side IPC BREAK!\n'
                            '#################################\n'
                        )

                        # TODO: other methods? see break func above.
                        # await stream._ctx.chan.send(None)
                        # await stream._ctx.chan.transport.stream.send_eof()
                        await stream._ctx.chan.transport.stream.aclose()

                        ipc_break_sent = True

                    # it actually breaks right here in the
                    # mp_spawn/forkserver backends and thus the zombie
                    # reaper never even kicks in?
                    print(f'parent sending {i}')
                    try:
                        await stream.send(i)
                    except ContextCancelled as ctxc:
                        print(
                            'parent received ctxc on `stream.send()`\n'
                            f'{ctxc}\n'
                        )
                        assert 'root' in ctxc.canceller
                        assert sub_name in ctx.canceller

                        # TODO: is this needed or no?
                        raise

                    # timeout: int = 1
                    # with trio.move_on_after(timeout) as cs:
                    async with stuff_hangin_ctlc() as timeout:
                        print(
                            f'PARENT `stream.receive()` with timeout={timeout}\n'
                        )
                        # NOTE: in the parent side IPC failure case this
                        # will raise an ``EndOfChannel`` after the child
                        # is killed and sends a stop msg back to it's
                        # caller/this-parent.
                        try:
                            rx = await stream.receive()
                            print(
                                "I'm a happy PARENT user and echoed to me is\n"
                                f'{rx}\n'
                            )
                        except trio.EndOfChannel:
                            rx_eoc: bool = True
                            print('MsgStream got EoC for PARENT')
                            raise

            print(
                'Streaming finished and we got Eoc.\n'
                'Canceling `.open_context()` in root with\n'
                'CTlR-C..'
            )
            if rx_eoc:
                assert stream.closed
                try:
                    await stream.send(i)
                    pytest.fail('stream not closed?')
                except (
                    trio.ClosedResourceError,
                    trio.EndOfChannel,
                ) as send_err:
                    if rx_eoc:
                        assert send_err is stream._eoc
                    else:
                        assert send_err is stream._closed

            raise KeyboardInterrupt


if __name__ == '__main__':
    trio.run(main)
