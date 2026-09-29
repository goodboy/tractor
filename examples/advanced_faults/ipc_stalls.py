'''
Test-owned IPC stall simulation and a separately signalled root.

These cancellable waits simulate runtime unresponsiveness, NOT
kernel TCP half-open connections, loss, or a shielded syscall stall.
Context-only faults wrap the OOB ``peer_ptl``'s separate ``Channel``
to the child, preserving cancellation through the subactor
``staller_ptl`` returned by ``an.start_actor()``. The mode
``stalled_context_and_subactor_cancel_send`` parks both context
directions and the subactor ``Portal``'s cancel send. Registry
traffic and the independent stdout control pipe remain usable.

'''
import argparse
from collections.abc import AsyncIterator
from functools import partial
import json
import os
from typing import Any

import trio
import tractor
from tricycle import BufferedReceiveStream
from tractor._testing import assert_child_reaped
from tractor._testing.addr import get_rando_addr
from tractor.discovery._addr import UnwrappedAddress
from tractor.ipc._chan import _connect_chan
from tractor.ipc._transport import MsgpackTransport
from tractor.msg.types import (
    Start,
    Yield,
)
from tractor.runtime._portal import open_portal


class StallableReceiveStream:
    '''
    Simulate a stalled buffered read on one IPC transport.

    ``MsgpackTransport._iter_packets()`` reads each packet header
    via ``MsgpackTransport.recv_stream.receive_exactly(4)``.
    Replacing ``MsgpackTransport.recv_stream`` intercepts that call,
    including reads satisfied without another socket operation.
    The packet iterator reads this field for each new header and
    payload. If an old call is already waiting inside the original
    receiver, the peer's responses finish that call first; its
    next read then goes through the replacement.

    '''
    def __init__(
        self,
        orig_recv: BufferedReceiveStream,
    ) -> None:
        '''
        Retain the receiver and allocate cancellable stall events.

        '''
        self.orig_recv: BufferedReceiveStream = orig_recv
        self.enabled: bool = False
        self.parked: trio.Event = trio.Event()
        self.release: trio.Event = trio.Event()
        self.receiving: int = 0

    async def receive_exactly(
        self,
        size: int,
    ) -> bytes:
        '''
        Pause a buffered read until the test releases or cancels it.

        Count active calls and set
        ``StallableReceiveStream.parked`` before waiting on
        ``StallableReceiveStream.release``. Once released, read
        from the original ``BufferedReceiveStream``.
        The wait stays cancellable for the SIGINT teardown test.

        '''
        if self.enabled:
            self.receiving += 1
            try:
                self.parked.set()
                await self.release.wait()
            finally:
                self.receiving -= 1
        return await self.orig_recv.receive_exactly(size)


class StallableTransport:
    '''
    Simulate stalled IPC sends and receives on one ``Channel``.

    Replace ``Channel._transport`` to pause sends before its
    production shield, and replace ``MsgpackTransport.recv_stream``
    with a ``StallableReceiveStream`` for buffered packet reads.
    Other transport operations delegate to the original
    ``MsgpackTransport``.
    The test can inspect each paused operation independently while
    the registry and unrelated actor channels remain usable.

    '''
    def __init__(
        self,
        channel: tractor.Channel,
        control_marker: str|None = None,
    ) -> None:
        '''
        Wrap one ``Channel``'s transport and buffered receiver.

        '''
        self.orig_tpt: MsgpackTransport = channel._transport
        self.recv_stream: StallableReceiveStream = (
            StallableReceiveStream(
                self.orig_tpt.recv_stream
            )
        )
        self.send_enabled: bool = False
        self.send_parked: trio.Event = trio.Event()
        self.release_send: trio.Event = trio.Event()
        self.sending: int = 0
        self.finite_send: bool = False
        self.cancel_request: bool = False
        self.control_marker: str|None = control_marker
        self.orig_tpt.recv_stream = self.recv_stream
        channel._transport = self

    def __getattr__(
        self,
        name: str,
    ) -> Any:
        '''
        Delegate other transport operations to the original.

        '''
        return getattr(self.orig_tpt, name)

    def __aiter__(self) -> AsyncIterator[dict]:
        '''
        Delegate packet iteration to ``StallableTransport.orig_tpt``.

        Its packet reader uses the replaced ``recv_stream`` field.

        '''
        return self.orig_tpt.__aiter__()

    async def send(
        self,
        msg: Any,
        send_deadline: float = float('inf'),
        **kwargs: Any,
    ) -> None:
        '''
        Park a send before ``MsgpackTransport.send()`` enters its
        shield.

        ``StallableTransport.send_parked`` tells the controller
        when a real send is blocked. The cancellable wait respects
        ``send_deadline``; a stalled cancel request can time out
        and trigger runtime hard-reaping.

        '''
        if self.send_enabled:
            self.sending += 1
            try:
                self.finite_send = send_deadline != float('inf')
                self.cancel_request = (
                    isinstance(msg, Start)
                    and
                    msg.ns == 'self'
                    and
                    msg.func == 'cancel'
                )
                self.send_parked.set()
                with trio.CancelScope(deadline=send_deadline) as cs:
                    await self.release_send.wait()
                if cs.cancelled_caught:
                    raise tractor.TransportClosed(
                        'Test IPC send deadline expired'
                    )
            finally:
                self.sending -= 1
                if self.control_marker:
                    emit(
                        marker=self.control_marker,
                        event='control_send_exit',
                        deadline_expired=(
                            trio.current_time() >= send_deadline
                        ),
                    )
        await self.orig_tpt.send(
            msg,
            send_deadline=send_deadline,
            **kwargs,
        )

    def snapshot(self) -> dict[str, bool]:
        '''
        Report which transport operations are still parked.

        '''
        return {
            'receive': bool(self.recv_stream.receiving),
            'send': bool(self.sending),
            'finite_send': self.finite_send,
        }


@tractor.context
async def probe_peer(
    ctx: tractor.Context,
) -> None:
    '''
    Keep the peer context alive while the caller stalls IPC.

    Two responses finish any pending read in the original
    receiver. ``MsgpackTransport._iter_packets()`` then reads
    again through the replaced ``recv_stream`` field, even when
    the previous response used buffered bytes. Only the external
    controller's SIGINT may terminate this test context.

    '''
    await ctx.started(os.getpid())
    async with ctx.open_stream() as stream:
        assert await stream.receive() == 'probe'
        await stream.send('probe-1')
        await stream.send('probe-2')
        await trio.sleep_forever()
    raise AssertionError('Peer stream completed before cancellation')


async def peer_address() -> UnwrappedAddress:
    '''
    Provide the peer address for a distinct OOB context connection.

    '''
    return tractor.current_actor().accept_addrs[0]


def emit(
    marker: str,
    event: str,
    **fields: Any,
) -> None:
    '''
    Publish a JSON test event on the root's stdout pipe.

    The controller in ``tests/test_ipc_stalls.py`` uses ``marker``
    to distinguish child identity, readiness and final results
    from actor logs.

    '''
    message: str = marker + json.dumps({
        'event': event,
        **fields,
    })
    print(message, flush=True)


async def root_main(
    # !TODO, explain each of these inputs with comments..
    mode: str,  # ``test_stalled_channel_sigint`` case to simulate
    port: int,  # isolated root actor registry port
    marker: str,  # prefix for controller-visible JSON records
    state: dict[str, Any],  # child and parked-I/O result evidence
) -> None:
    '''
    Park selected IPC operations in a test-owned actor tree.

    ``mode`` selects ``healthy_ipc``, ``stalled_context_receive``,
    ``stalled_context_send``, ``stalled_context_send_and_receive``,
    ``stalled_context_cancel_send``, or
    ``stalled_context_and_subactor_cancel_send``. The last case
    parks both context directions and the subactor ``Portal``'s
    cancel request. ``port`` selects this root's registry address;
    ``marker`` frames stdout events; ``state`` retains child and
    parked-I/O evidence for the SIGINT result.

    '''
    async with tractor.open_root_actor(
        name='ipc-stall-root',
        registry_addrs=[('127.0.0.1', port)],
        enable_transports=['tcp'],
        ensure_registry=True,
        start_method='trio',
        debug_mode=False,
        loglevel='error',
    ):
        async with tractor.open_nursery() as an:
            staller_ptl = await an.start_actor(
                'ipc-stall-peer',
                enable_modules=[__name__],
            )
            proc: trio.Process = next(
                iter(an._children.values())
            )[1]
            state['child'] = proc
            emit(
                marker=marker,
                event='child',
                pid=proc.pid,
            )
            peer_addr: UnwrappedAddress = await staller_ptl.run(
                peer_address
            )

            # Open an OOB context ``Channel`` via ``_connect_chan``
            # and ``open_portal`` as ``peer_ptl``. The subactor
            # ``Portal``'s ``staller_ptl.channel`` is distinct.
            #
            # Most modes leave subactor ``Portal`` cancel usable.
            # ``stalled_context_and_subactor_cancel_send`` also
            # stalls its cancel send AND both context directions.
            async with (
                _connect_chan(peer_addr) as peer_channel,
                open_portal(peer_channel) as peer_ptl,
                peer_ptl.open_context(probe_peer) as (ctx, pid),
            ):
                # Match the peer PID from ``Context.started()``.
                assert pid == proc.pid
                # The OOB context connection must not silently
                # stall the subactor ``Portal``'s channel as well.
                assert ctx.chan is not staller_ptl.channel
                stall_tpt: StallableTransport = StallableTransport(
                    ctx.chan
                )
                stall_tpt.recv_stream.enabled = mode in {
                    # Suspend context receive only.
                    'stalled_context_receive',
                    # Suspend context send and receive.
                    'stalled_context_send_and_receive',
                    # Also stall the subactor ``Portal`` cancel send.
                    'stalled_context_and_subactor_cancel_send',
                }
                await ctx.chan.send(
                    Yield(cid=ctx.cid, pld='probe')
                )
                # Wait for the packet reader to park for each
                # receive-stalled mode, not just wrapper setup.
                # ?TODO, in what cases is this set vs. not?
                # sure would be handy to see that from this layer..
                # ``stalled_context_receive``,
                # ``stalled_context_send_and_receive``, and
                # ``stalled_context_and_subactor_cancel_send`` wait
                # here; other cases consume both probe responses.
                if stall_tpt.recv_stream.enabled:
                    await stall_tpt.recv_stream.parked.wait()
                else:
                    # Drain the peer's two responses in modes that
                    # do not intentionally stall context reads.
                    for expected in ('probe-1', 'probe-2'):
                        pld: str = await ctx.pld_rx.recv_pld(
                            ipc=ctx,
                            expect_msg=Yield,
                        )
                        assert pld == expected

                stall_tpt.send_enabled = mode in {
                    # Suspend context send only.
                    'stalled_context_send',
                    # Suspend both context directions.
                    'stalled_context_send_and_receive',
                    # Suspend ``Context.cancel()``'s timed send.
                    'stalled_context_cancel_send',
                    # Also suspend the subactor ``Portal`` cancel.
                    'stalled_context_and_subactor_cancel_send',
                }
                control_tpt: StallableTransport|None = None
                if mode == (
                    'stalled_context_and_subactor_cancel_send'
                ):
                    control_tpt = StallableTransport(
                        staller_ptl.channel,
                        control_marker=marker,
                    )
                    control_tpt.send_enabled = True

                async def ready() -> None:
                    '''
                    Report selected parked I/O to the controller.

                    Readiness requires real blocked sends/receives;
                    simply installing a wrapper would not prove a
                    stalled connection before SIGINT arrives.

                    '''
                    if stall_tpt.send_enabled:
                        await stall_tpt.send_parked.wait()
                    if control_tpt is not None:
                        await control_tpt.send_parked.wait()
                        assert control_tpt.cancel_request
                        assert control_tpt.finite_send
                        assert control_tpt.sending
                        state['control_fault_active'] = (
                            control_tpt.snapshot()
                        )
                    snapshot: dict[str, bool] = stall_tpt.snapshot()
                    assert snapshot['receive'] == (
                        stall_tpt.recv_stream.enabled
                    )
                    assert snapshot['send'] == stall_tpt.send_enabled
                    if mode == 'stalled_context_cancel_send':
                        assert snapshot['finite_send']
                    assert proc.returncode is None
                    assert ctx.chan.connected()
                    state['stalls_fault_active'] = snapshot
                    emit(
                        marker=marker,
                        event='ready',
                        stalls=snapshot,
                        control=state.get(
                            'control_fault_active'
                        ),
                    )

                async with trio.open_nursery() as tn:
                    tn.start_soon(ready)
                    if mode == 'stalled_context_cancel_send':
                        try:
                            pass
                        finally:
                            # Actual teardown publication, with its
                            # normal finite deadline, not a sleep.
                            await ctx.cancel(timeout=5)
                        raise AssertionError(
                            'Cleanup finished before SIGINT'
                        )
                    if stall_tpt.send_enabled:
                        tn.start_soon(
                            ctx.chan.send,
                            Yield(cid=ctx.cid, pld='blocked'),
                        )
                    if control_tpt is not None:
                        await staller_ptl.cancel_actor(timeout=5)
                        raise AssertionError(
                            'Control cancel finished before SIGINT'
                        )
                    await trio.sleep_forever()
            raise AssertionError('Context completed before SIGINT')


def main() -> None:
    '''
    Run the root in a process separate from the test controller.

    ``tests/test_ipc_stalls.py`` passes ``mode``, ``port``, and
    ``marker`` as positional CLI args to run this example in a
    separate process. It sends SIGINT without signalling ``pytest``
    and observes JSON events and process exit through stdout and
    its OS process handle, not a potentially stalled ``Channel``.
    Running this module directly defaults to
    ``stalled_context_receive`` and selects an isolated registry
    port with ``tractor._testing.addr.get_rando_addr()``.

    '''
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'mode', nargs='?', default='stalled_context_receive',
        choices=(
            'healthy_ipc',
            'stalled_context_receive',
            'stalled_context_send',
            'stalled_context_send_and_receive',
            'stalled_context_cancel_send',
            'stalled_context_and_subactor_cancel_send',
        ),
    )
    parser.add_argument('port', type=int, nargs='?')
    parser.add_argument(
        'marker', nargs='?', default='IPC-STALL:',
    )
    args = parser.parse_args()
    port: int|None = args.port
    if port is None:
        _, port = get_rando_addr('tcp')

    state: dict[str, Any] = {}
    try:
        trio.run(
            partial(
                root_main,
                mode=args.mode,
                port=port,
                marker=args.marker,
                state=state,
            ),
            restrict_keyboard_interrupt_to_checkpoints=True,
        )
    except KeyboardInterrupt:
        proc: trio.Process = state['child']
        assert_child_reaped(proc)
        # A returncode alone is insufficient: prove the runtime
        # waited for its child rather than leaving a zombie behind.
        try:
            waited = os.waitpid(proc.pid, os.WNOHANG)
        except ChildProcessError:
            reaped = True
        else:
            raise AssertionError(
                ('Runtime did not reap child', waited)
            )
        emit(
            marker=args.marker,
            event='result',
            root_exit='KeyboardInterrupt',
            stalls_fault_active=state['stalls_fault_active'],
            child_reaped=reaped,
            child_returncode=proc.returncode,
            control_fault_active=state.get('control_fault_active'),
        )
    else:
        raise AssertionError('Root exited without KeyboardInterrupt')


if __name__ == '__main__':
    main()
