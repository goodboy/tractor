'''
Exercise the proposed discovered-peer EOF notification policy.

'''
from unittest.mock import Mock

import pytest
import trio
from trio.testing import wait_all_tasks_blocked

import tractor
from tractor._exceptions import TransportClosed
from tractor.msg.types import Return


_streams: dict[str, tractor.Context] = {}


@tractor.context
async def stream_peer(
    ctx: tractor.Context,
) -> None:
    '''
    Publish a live stream and echo until its caller finishes.

    '''
    _streams[ctx.cid] = ctx
    await ctx.started()
    async with ctx.open_stream() as stream:
        await stream.send('ready')
        async for value in stream:
            if value == 'finish':
                break
            await stream.send(value)


async def send_peer_eof(cid: str) -> None:
    '''
    Close only this test channel's outgoing transport half.

    This helper sends no Stop, Return, or Error. Keeping the read
    half open avoids a local fd-close error crashing the peer actor
    and confusing transport propagation with supervision.

    '''
    ctx = _streams[cid]
    await ctx.chan._transport.stream.send_eof()


@tractor.context
async def consume_peer(
    owner: tractor.Context,
    wait_on: str,
    reg_addr,
) -> bool:
    '''
    Exercise a sibling connection outside child supervision.

    '''
    with trio.fail_after(10):
        async with (
            # Discovery creates a sibling channel, independent of
            # the root actor's control connection to eof_peer.
            tractor.find_actor(
                'eof_peer',
                registry_addrs=[reg_addr],
            ) as peer,
            # This second peer is a control: the fault on peer's
            # channel must not terminate unrelated communications.
            tractor.find_actor(
                'healthy_peer',
                registry_addrs=[reg_addr],
            ) as healthy,
            healthy.open_context(stream_peer) as (good, _),
            good.open_stream() as good_stream,
            trio.open_nursery() as tn,
        ):
            # owner.chan carries the root's orchestration messages;
            # losing peer.channel must not lose the result channel.
            assert peer.channel is not owner.chan
            # Confirm the healthy context is live before injecting
            # the fault, not merely connect to it after teardown.
            assert await good_stream.receive() == 'ready'
            errors: list[TransportClosed] = []

            # Set only once BOTH affected context owners observe
            # TransportClosed; one notified owner is insufficient.
            finished = trio.Event()

            # Model an application checkpoint unrelated to IPC.
            # Never release it for fault cases: only the proposed
            # context-scope cancellation may interrupt that wait.
            # Completed contexts are explicitly released after EOF.
            paused = trio.Event()

            # A local report hook confirms process_messages handled
            # EOF before we inspect a completed context's outcome.
            eof_seen = trio.Event()
            graceful = wait_on in {'returned', 'buffered'}
            report = TransportClosed.report_n_maybe_raise

            def report_eof(exc, *args, **kwargs):
                # During this gate only the injected peer channel
                # closes; healthy/control teardown happens later.
                eof_seen.set()
                return report(exc, *args, **kwargs)

            # This sibling is a fresh test actor; observe the real
            # handler without changing its exception policy.
            TransportClosed.report_n_maybe_raise = report_eof

            async def consume(
                task_status=trio.TASK_STATUS_IGNORED,
            ):
                try:
                    async with (
                        peer.open_context(stream_peer) as (ctx, _),
                        ctx.open_stream() as stream,
                    ):
                        assert await stream.receive() == 'ready'
                        if graceful:
                            # Observe Context._deliver_msg receipt,
                            # without requiring the owner to drain
                            # the final result from its feeder.
                            returned = trio.Event()
                            deliver = ctx._deliver_msg

                            async def record_return(msg):
                                result = await deliver(msg)
                                if isinstance(msg, Return):
                                    returned.set()
                                return result

                            ctx._deliver_msg = record_return
                            # Ask the remote context to finish while
                            # leaving its shared actor channel open.
                            await stream.send('finish')
                            await returned.wait()
                            if wait_on == 'returned':
                                assert (
                                    await ctx.wait_for_result()
                                ) is None
                        # Export the cid only after stream startup
                        # and any graceful Return delivery. The root
                        # uses it to select the faulted connection.
                        task_status.started(ctx)
                        if graceful:
                            await paused.wait()
                            assert ctx._status._remote_error is None
                        elif wait_on == 'receive':
                            await stream.receive()
                        else:
                            await paused.wait()
                        if not graceful:
                            pytest.fail('resumed without EOF')
                except TransportClosed as exc:
                    assert not graceful
                    assert exc is ctx._status._remote_error
                    assert exc.__cause__ is exc._src_exc
                    assert 'closed' in exc.message
                    # This is a local transport observation, not
                    # evidence that a peer requested cancellation.
                    assert ctx.canceller is None
                    assert not ctx.cancel_called
                    assert not ctx.cancel_acked
                    errors.append(exc)
                    if len(errors) == 2:
                        finished.set()

            # Both contexts share one discovered channel but own
            # separate local scopes. EOF must notify both owners.
            first = await tn.start(consume)
            second = await tn.start(consume)
            assert first.chan is second.chan
            # Neither readiness handshake alone proves the owner is
            # blocked at the intended checkpoint. Quiescence does.
            await wait_all_tasks_blocked()
            original_send = peer.channel.send
            checking_eof = True

            async def send_after_notification(*args, **kwargs):
                # Cleanup may send, but must not discover the EOF.
                if checking_eof:
                    assert first._status._remote_error is not None
                    assert second._status._remote_error is not None
                return await original_send(*args, **kwargs)

            if not graceful:
                peer.channel.send = send_after_notification
            # Release the root's fault injection only after both
            # owners are waiting and the no-send-detection guard is
            # installed. The root uses a separate control channel.
            await owner.started(first.cid)
            try:
                with trio.fail_after(2):
                    await eof_seen.wait()
                    if graceful:
                        paused.set()
                    else:
                        await finished.wait()
            finally:
                checking_eof = False
            assert len(errors) == (0 if graceful else 2)
            assert paused.is_set() is graceful
            assert good._status._remote_error is None
            # Prove the unrelated peer still serves application
            # traffic after EOF handling, not just that it has no
            # recorded error yet.
            await good_stream.send('still alive')
            assert await good_stream.receive() == 'still alive'
    return True


@pytest.mark.parametrize(
    'wait_on',
    [
        # Unfinished context blocked in its receive API: wake it
        # without requiring an outbound write to discover EOF.
        'receive',

        # Unfinished context blocked on an application Event:
        # exercise the proposed interruption of non-IPC awaits.
        'event',

        # Final Return already consumed: preserve success when the
        # connection closes after its remote task has completed.
        'returned',

        # Final Return received but not drained: preserve the same
        # success even though the owner has not requested its result.
        'buffered',
    ],
)
def test_discovered_peer_eof(
    reg_addr,
    wait_on: str,
) -> None:
    '''
    Exercise the proposed EOF-driven cancellation of context owners.

    Previously process_messages logged TransportClosed and returned,
    leaving parent Context scopes alive until a later outbound send.
    Two owners share a discovered channel, independently of the
    ActorNursery's child-control channel. Both receive an initial
    stream item, then block in receive() or on an unset Event.
    Nursery.start() and wait_all_tasks_blocked() establish that
    ordering before a control RPC half-closes the peer transport.

    Under the proposed policy both owners must raise TransportClosed
    before the deadline without sending to discover the failure.
    The Event case deliberately asserts interruption of a non-IPC
    checkpoint; whether to adopt that contract remains under review.
    An unrelated peer stream must still echo afterward and close
    gracefully. The original EOF cause must survive unwinding.

    Graceful variants deliver Return before EOF, either draining its
    value or leaving it buffered while the owner pauses. Observing
    delivery and the EOF report synchronizes those races without a
    timing sleep. Neither completed owner may be cancelled by EOF.

    '''
    async def main():
        async with (
            tractor.open_nursery(
                registry_addrs=[reg_addr],
            ) as an,
        ):
            # Retain a root-to-peer portal exclusively for injecting
            # EOF. Its transport is not the sibling channel tested.
            control = await an.start_actor(
                'eof_peer',
                enable_modules=[__name__],
            )
            await an.start_actor(
                'healthy_peer',
                enable_modules=[__name__],
            )
            # A separate consumer ensures failure cannot propagate
            # merely through the root's child-supervision nursery.
            consumer = await an.start_actor(
                'consumer',
                enable_modules=[__name__],
            )
            async with consumer.open_context(
                consume_peer,
                wait_on=wait_on,
                reg_addr=reg_addr,
            ) as (ctx, cid):
                # consume_peer exports cid after both owners are
                # blocked; only then break their shared connection.
                await control.run(send_peer_eof, cid=cid)
                assert await ctx.wait_for_result() is True

            await an.cancel()

    trio.run(main)


def test_disconnect_cause_survives_unwind() -> None:
    '''
    Preserve the transport cause when cancelling a context owner.

    Context result/stream receivers can supply their cancellation as
    from_src_exc while unwinding. That must not replace the original
    socket error in TransportClosed.__cause__. Exercise the actual
    raise path with a reset cause and a distinct unwinding exception;
    both exception identity and the original cause must survive.

    '''
    ctx = Mock()
    ctx._is_self_cancelled.return_value = False
    cause = ConnectionResetError('test peer reset')
    error = TransportClosed('test disconnect', src_exc=cause)
    with pytest.raises(TransportClosed) as raised:
        tractor.Context._maybe_raise_remote_err(
            ctx,
            error,
            from_src_exc=RuntimeError('owner unwinding'),
        )
    assert raised.value is error
    assert error.__cause__ is cause
    assert error.src_exc is cause
