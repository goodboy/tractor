'''
Isolate final-result delivery from sibling service lifetimes.

'''
from uuid import uuid4

import msgspec
import pytest
import tractor
import trio

from tractor.ipc import Channel
from tractor.msg import Aid, StartAck, Started
from tractor.runtime import _rpc


@tractor.context
async def finish_after_disconnect(
    ctx: tractor.Context,
    peer_closed: trio.Event,
    endpoint_finished: trio.Event,

) -> str:
    '''
    Finish successfully only after the caller connection is closed.

    '''
    await ctx.started('ready')
    await peer_closed.wait()
    endpoint_finished.set()
    return 'finished'


async def ordinary_result(
    peer_closed: trio.Event,
    endpoint_finished: trio.Event,

) -> str:
    '''
    Exercise the already-hardened ordinary RPC final-result path.

    '''
    await peer_closed.wait()
    endpoint_finished.set()
    return 'finished'


@pytest.mark.parametrize(
    'context_endpoint',
    [False, True],
    ids=['ordinary-rpc', 'context'],
)
def test_final_return_disconnect_preserves_sibling(
    reg_addr: tuple,
    context_endpoint: bool,

) -> None:
    '''
    A departing caller must not cancel an unrelated service task.

    Chart Ctrl-C made open_symbol_search finish before Tractor sent
    its final Return. TransportClosed from that send escaped _invoke
    and cancelled the shared service nursery, including IB feeds.

    Use real loopback TCP channels and a real Actor/Context, invoking
    _invoke directly to control the precise response-send ordering.
    The caller consumes startup messages, closes its socket, and the
    callee observes EOF and closes its channel before releasing the
    successful endpoint. No transport exception is mocked or injected.

    After _invoke completes, a sibling must still answer a request.
    The ordinary-RPC case is a control for the existing response-send
    hardening. The context case exposes the uncovered final Return.
    Capture the sibling's task, actor, nursery and cancellation reprs
    while unwinding so the failure identifies the collateral victim
    and the cancellation source even after the runtime has exited.
    This isolates response delivery; it does not exercise discovery,
    process supervision, the asyncio bridge, or Qt cancellation.

    '''
    collateral_cancellations: list[tuple[trio.Cancelled, str]] = []

    async def main() -> None:
        '''
        Order peer EOF before endpoint completion without sleeps.

        '''
        peer_closed = trio.Event()
        endpoint_finished = trio.Event()
        invoke_finished = trio.Event()
        ping = trio.Event()
        pong = trio.Event()
        sibling_stopped = trio.Event()

        async def sibling(
            task_status: trio.TaskStatus = trio.TASK_STATUS_IGNORED,
        ) -> None:
            '''
            Answer a post-disconnect request until explicitly stopped.

            '''
            task_status.started()
            try:
                await ping.wait()
                pong.set()
                await trio.sleep_forever()
            except trio.Cancelled as exc:
                if not pong.is_set():
                    assert endpoint_finished.is_set()
                    task: trio.lowlevel.Task = (
                        trio.lowlevel.current_task()
                    )
                    aid: Aid = actor.aid
                    nursery: trio.Nursery = task.parent_nursery
                    cancel_scope: trio.CancelScope = (
                        nursery.cancel_scope
                    )
                    runtime_info: str = (
                        f'\nSibling task: {task!r}\n'
                        f'Actor identity: {aid!r}\n'
                        f'Parent nursery: {nursery!r}\n'
                        f'Parent cancel scope: {cancel_scope!r}\n'
                        f'Cancellation: {exc!r}'
                    )
                    collateral_cancellations.append(
                        (exc, runtime_info),
                    )
                raise
            finally:
                sibling_stopped.set()

        with trio.fail_after(10):
            async with tractor.open_root_actor(
                registry_addrs=[reg_addr],
                enable_transports=['tcp'],
                loglevel='error',
            ) as actor:
                listeners = await trio.open_tcp_listeners(
                    0,
                    host='127.0.0.1',
                )
                [listener] = listeners
                async with listener:
                    port: int = listener.socket.getsockname()[1]
                    caller_stream = await trio.open_tcp_stream(
                        '127.0.0.1',
                        port,
                    )
                    callee_stream = await listener.accept()

                async with caller_stream, callee_stream:
                    caller = Channel.from_stream(caller_stream)
                    callee = Channel.from_stream(callee_stream)
                    caller._aid = actor.aid
                    callee._aid = Aid(
                        name='departing-chart',
                        uuid=str(uuid4()),
                    )
                    cid: str = str(uuid4())
                    endpoint = (
                        finish_after_disconnect
                        if context_endpoint else ordinary_result
                    )
                    kwargs = {
                        'peer_closed': peer_closed,
                        'endpoint_finished': endpoint_finished,
                    }

                    async def invoke(
                        task_status: trio.TaskStatus,
                    ) -> None:
                        '''
                        Run the actual RPC dispatcher to completion.

                        '''
                        await _rpc._invoke(
                            actor,
                            cid,
                            callee,
                            endpoint,
                            kwargs,
                            task_status=task_status,
                        )
                        invoke_finished.set()

                    async with trio.open_nursery() as services:
                        await services.start(sibling)
                        ctx = await services.start(invoke)
                        ack = await caller.recv()
                        assert isinstance(ack, StartAck)
                        assert ack.cid == cid
                        if context_endpoint:
                            started = await caller.recv()
                            assert isinstance(started, Started)
                            assert started.cid == cid
                            assert msgspec.msgpack.decode(
                                started.pld,
                            ) == 'ready'

                        await caller.aclose()
                        with pytest.raises(tractor.TransportClosed):
                            await callee.recv()
                        await callee.aclose()
                        peer_closed.set()

                        await invoke_finished.wait()
                        assert endpoint_finished.is_set()
                        assert not sibling_stopped.is_set()
                        assert (callee, cid) not in actor._rpc_tasks
                        ping.set()
                        await pong.wait()
                        if context_endpoint:
                            assert ctx._result == 'finished'
                        services.cancel_scope.cancel()

    try:
        trio.run(main)
    except tractor.TransportClosed as exc:
        assert collateral_cancellations
        cancelled, runtime_info = collateral_cancellations[0]
        assert 'TransportClosed' in str(cancelled)
        exc.add_note(
            'The unrelated sibling was cancelled before it could '
            'answer the post-disconnect request.'
            + runtime_info
        )
        raise
