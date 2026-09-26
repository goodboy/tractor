'''
Detect a closed IPC transport during real-time streaming.

Detected closure must fail an unfinished context promptly, including
when the peer sent a stream stop first. Only the no-break example
simulates a user pressing Ctrl-C to cancel a healthy actor tree.

'''
from contextlib import asynccontextmanager as acm

from tractor import (
    open_nursery,
    context,
    Context,
    _testing,
)
import trio


@context
async def recv_and_spawn_net_killers(
    ctx: Context,
    break_ipc_after: bool|int = False,
    pre_close: bool = False,
) -> None:
    '''
    Echo messages until a single, sequence-numbered IPC break.

    Break before echoing the trigger so the caller is waiting for
    that reply, rather than racing another send against peer close.
    Keep this context unfinished: stream stop is not a final result.

    '''
    await ctx.started()
    async with ctx.open_stream() as stream:
        async for i in stream:
            if (
                break_ipc_after
                and
                i >= break_ipc_after
            ):
                if pre_close:
                    await stream.aclose()
                    # Protocol Stop closes the stream, not its IPC.
                    assert stream.closed
                    assert ctx.chan.connected()
                await _testing.break_ipc(
                    stream=stream,
                    pre_close=False,
                )
                await trio.sleep_forever()

            print(f'child echoing {i}')
            await stream.send(i)


@acm
async def stuff_hangin_ctlc(timeout: float = 1) -> None:
    '''
    Simulate Ctrl-C when a healthy stream makes no progress.

    Detected-fault runs use a failing deadline instead, so a timeout
    cannot masquerade as successful transport-failure propagation.

    '''
    with trio.move_on_after(timeout) as cs:
        yield timeout

    if cs.cancelled_caught:
        # Pretend to be a user seeing no streaming action,
        # thinking it's a hang, and then hitting Ctrl-C.
        print(
            f'PARENT streaming stalled after {timeout}s\n'
            f'\n'
            f'MASHING Ctrl-C..!\n'
        )
        raise KeyboardInterrupt


async def main(
    debug_mode: bool = False,
    start_method: str = 'trio',
    loglevel: str = 'cancel',

    # By default the parent breaks first, if configured to break
    # at all. Reverse the thresholds to make the child break first.
    break_parent_ipc_after: int|bool = False,
    break_child_ipc_after: int|bool = False,
    pre_close: bool = False,
    tpt_proto: str = 'tcp',
    reg_addr: tuple[str, int|str]|None = None,
) -> None:
    '''
    Run a bounded close scenario or demonstrate Ctrl-C cancellation.

    Tests pass their unique registrar address explicitly. Standalone
    invocation retains the runtime's default address selection.

    '''
    registry_kwargs = {}
    if reg_addr is not None:
        registry_kwargs = {
            'registry_addrs': [reg_addr],
            'ensure_registry': True,
        }

    async with open_nursery(
        start_method=start_method,
        # Even with debugging enabled, broken IPC must not hang
        # trying to engage a debugger through the closed channel.
        debug_mode=debug_mode,
        loglevel=loglevel,
        enable_transports=[tpt_proto],
        **registry_kwargs,
    ) as an:
        portal = await an.start_actor(
            'chitty_hijo',
            enable_modules=[__name__],
        )
        breaking = (
            break_parent_ipc_after
            or
            break_child_ipc_after
        )
        async with portal.open_context(
            recv_and_spawn_net_killers,
            break_ipc_after=break_child_ipc_after,
            pre_close=pre_close,
        ) as (ctx, sent):
            # A transport failure is not a remote cancellation RPC;
            # do not require ContextCancelled or invent a canceller.
            with trio.fail_after(3):
                async with ctx.open_stream() as stream:
                    for i in range(1000):
                        if (
                            break_parent_ipc_after
                            and
                            i >= break_parent_ipc_after
                        ):
                            # TODO: other methods? See break_ipc().
                            # await ctx.chan.send(None)
                            socket = ctx.chan.transport.stream
                            # await socket.send_eof()
                            await socket.aclose()
                            assert not ctx.chan.connected()
                            await trio.sleep_forever()

                        await stream.send(i)
                        if breaking:
                            rx = await stream.receive()
                        else:
                            async with stuff_hangin_ctlc():
                                rx = await stream.receive()
                        assert rx == i

                if breaking:
                    # open_stream() absorbs a peer's stop/EoC, but
                    # open_context() still needs a result. EOF must
                    # wake this wait without a later user SIGINT.
                    await ctx.wait_for_result()
                    raise AssertionError('broken ctx returned?')

                raise KeyboardInterrupt


if __name__ == '__main__':
    trio.run(main)
