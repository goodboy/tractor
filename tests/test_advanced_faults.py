'''
Sketchy network blackoutz, ugly byzantine gens, puedes eschuchar la
cancelacion?..

'''
from contextlib import asynccontextmanager as acm
from types import ModuleType

import pytest
from _pytest.pathlib import import_path
import trio
import tractor
from tractor import (
    Portal,
    TransportClosed,
)
from tractor.runtime._supervise import ActorNursery
from tractor._testing import (
    examples_dir,
    break_ipc,
    assert_child_reaped,
)


@pytest.fixture
def reaped_children(monkeypatch: pytest.MonkeyPatch):
    '''
    Retain spawned process handles and check synchronous reaping.

    Snapshot each ActorNursery._children entry at publication, before
    failure teardown removes it. Check both backend completion and
    PID disappearance after trio.run(), without an external reaper.

    '''
    procs = []
    start_actor = ActorNursery.start_actor

    async def record_child(an, *args, **kwargs):
        '''
        Record only children spawned by this test's actor nursery.

        '''
        portal = await start_actor(an, *args, **kwargs)
        procs.append(an._children[portal.channel.aid.uid][1])
        return portal

    monkeypatch.setattr(ActorNursery, 'start_actor', record_child)
    yield procs
    for proc in procs:
        assert_child_reaped(proc)


@pytest.fixture
def fault_contexts(monkeypatch: pytest.MonkeyPatch):
    '''
    Observe fault state without altering cancellation or propagation.

    Collect the entered `tractor.Context`s in a `list` delivered
    to the consuming test. This allows introspecting a ctx's recorded
    fault even if teardown incorrectly swallows that failure.

    On teardown ensure transport-loss does not set
    `ContextStatus._canceller`: connection loss alone supplies no
    remote cancellation or task-error provenance. For the simulated
    user-interrupt cases, ensure `Context.cancel_called` is True
    because teardown requested cancellation of the remote task.

    '''
    contexts: list[tractor.Context] = []
    open_context = Portal.open_context

    @acm
    async def record_context(portal, func, **kwargs):
        '''
        Hook into all `Portal.open_context() as pair` calls, stashing
        their contexts in a `list` for consumption by dependent
        tests for introspection.

        '''
        ctx: tractor.Context|None = None
        try:
            async with open_context(
                portal,
                func,
                **kwargs,
            ) as (ctx, sent):
                if func.__name__ in {
                    'recv_and_spawn_net_killers',
                    'break_ipc_after_started',
                }:
                    contexts.append(ctx)
                yield (ctx, sent)
        finally:
            if (
                ctx in contexts
                and
                isinstance(ctx.maybe_error, TransportClosed)
            ):
                # MsgStream closure is distinct from transport loss.
                # After teardown, send prioritizes the ctx error,
                # while receive retains the stream's terminal marker.
                stream = ctx._stream
                assert stream.closed
                assert not ctx.chan.connected()
                with pytest.raises(TransportClosed) as err:
                    await stream.send('after-close')
                assert err.value is ctx.maybe_error
                terminal = stream._eoc or stream._closed
                assert isinstance(terminal, BaseException)
                with pytest.raises(type(terminal)) as err:
                    await stream.receive()
                assert err.value is terminal

    monkeypatch.setattr(Portal, 'open_context', record_context)
    yield contexts
    for ctx in contexts:
        if isinstance(ctx.maybe_error, TransportClosed):
            assert ctx.canceller is None
        elif isinstance(ctx._status._local_error, KeyboardInterrupt):
            assert ctx.cancel_called


def assert_transport_closed(
    exc: TransportClosed|BaseExceptionGroup,
    local: bool,
) -> tuple[TransportClosed, ...]:
    '''
    Validate every failure leaf, including its socket-level source.

    Parent close interrupts receive with ClosedResourceError.
    Peer close can report clean EOF or a reset, depending on buffered
    traffic. No arbitrary exception or remote cancellation qualifies.

    '''
    if isinstance(exc, BaseExceptionGroup):
        return tuple(
            fault
            for leaf in exc.exceptions
            for fault in assert_transport_closed(leaf, local=local)
        )

    assert isinstance(exc, TransportClosed)
    cause = exc.__cause__
    assert exc.src_exc is cause
    if local:
        assert isinstance(cause, trio.ClosedResourceError)
        assert 'closed locally' in str(exc)
    elif cause is None:
        assert 'gracefully closed' in str(exc)
    else:
        assert 'closed by peer' in str(exc)
        assert isinstance(
            cause,
            (ValueError, trio.BrokenResourceError,
             ConnectionResetError),
        )
        if isinstance(cause, ValueError):
            assert 'unclean EOF' in str(cause)

    # TODO, should we expect a certain exc-message (per tpt) too?
    # e.g. cause.args[0] == 'another task closed this fd'
    return (exc,)


@pytest.mark.parametrize(
    'pre_aclose_msgstream',
    [False, True],
    ids=['no_msgstream_aclose', 'pre_aclose_msgstream'],
)
@pytest.mark.parametrize(
    'ipc_break',
    [
        # No breaks: retain the healthy Ctrl-C cancellation contract.
        dict(break_parent_ipc_after=False,
             break_child_ipc_after=False),

        # Only parent breaks.
        dict(break_parent_ipc_after=500,
             break_child_ipc_after=False),

        # Only child breaks.
        dict(break_parent_ipc_after=False,
             break_child_ipc_after=500),

        # Both configured: parent breaks first.
        dict(break_parent_ipc_after=500,
             break_child_ipc_after=800),

        # Both configured: child breaks first.
        dict(break_parent_ipc_after=800,
             break_child_ipc_after=500),
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
    ipc_break: dict,
    pre_aclose_msgstream: bool,
    tpt_proto: str,
    reg_addr: tuple[str, int|str],
    reaped_children,
    fault_contexts: list[tractor.Context],
):
    '''
    Detect local/peer closure without waiting for a later Ctrl-C.

    The old scenario required ContextCancelled and a hang even when
    EOF had already notified the unfinished context. Sequence-number
    thresholds choose the first breaker; the child closes before its
    trigger reply, preventing the parent from advancing past it.

    With pre-close, MsgStream absorbs the peer's `Stop`, but pending
    Context.wait_for_result() must fail on transport loss. Check each
    exception leaf and its source, bound teardown, and verify the
    child is gone synchronously. No-break runs still raise synthetic
    KeyboardInterrupt and reap the child through normal cancellation.

    If Context._scope absorbs EOF-driven task cancellation, teardown
    must raise the recorded fault despite a disconnected channel.
    That path must not manufacture a cancel request or peer ack, and
    must preserve the original TransportClosed object and its cause.

    The fixture checks stream closure separately from transport
    connectivity after unwinding, then retries send and receive to
    verify their distinct error precedence and terminal identities.

    '''
    if spawn_backend != 'trio' and debug_mode:
        pytest.skip('debug_mode only supported on trio spawner')

    mod: ModuleType = import_path(
        examples_dir() / 'advanced_faults'
        / 'ipc_failure_during_stream.py',
        root=examples_dir(),
        consider_namespace_packages=False,
    )
    parent = ipc_break['break_parent_ipc_after']
    child = ipc_break['break_child_ipc_after']
    breaking = parent or child
    local = bool(
        parent
        and
        (
            (not child)
            or
            (parent < child)
        )
    )

    async def run():
        '''
        Bound the example, including actor nursery teardown.

        '''
        with trio.fail_after(10):
            await mod.main(
                debug_mode=debug_mode,
                start_method=spawn_backend,
                loglevel=loglevel,
                pre_close=pre_aclose_msgstream,
                tpt_proto=tpt_proto,
                reg_addr=reg_addr,
                **ipc_break,
            )

    if not breaking:
        with pytest.raises(KeyboardInterrupt):
            trio.run(run)
    else:
        with pytest.raises((TransportClosed, ExceptionGroup)) as err:
            trio.run(run)
            ctx = fault_contexts[0]
            pytest.fail(
                'Transport failure was swallowed:\n'
                f'{ctx.repr_outcome()}\n'
                f'error={ctx.maybe_error!r}'
            )
        faults = assert_transport_closed(err.value, local=local)
        ctx = fault_contexts[0]
        assert any(fault is ctx.maybe_error for fault in faults)
        if ctx._scope.cancelled_caught:
            error = ctx._status._remote_error
            assert any(fault is error for fault in faults)
            assert ctx._status._local_error is error
            assert not ctx.cancel_called
            assert not ctx.cancel_acked
    assert len(reaped_children) == len(fault_contexts) == 1


@tractor.context
async def break_ipc_after_started(
    ctx: tractor.Context,
) -> None:
    '''
    Wait for the caller's stream barrier, then close unfinished IPC.

    '''
    await ctx.started()
    async with ctx.open_stream() as stream:
        assert await stream.receive() == 'ready'

        # TODO: make a test which verifies the error
        # for this, i.e. raises a `MsgTypeError`
        # await ctx.chan.send(None)

        await break_ipc(stream=stream, pre_close=True)
        await trio.sleep_forever()


def test_stream_closed_right_after_ipc_break_and_zombie_lord_engages(
    reg_addr: tuple[str, int|str],
    tpt_proto: str,
    spawn_backend: str,
    loglevel: str,
    reaped_children,
    fault_contexts,
):
    '''
    Reap a child after detected close without a later user SIGINT.

    Previously the parent slept, timed out its context, then raised
    KeyboardInterrupt to engage process supervision ("zombie lord").
    EOF now notifies the unfinished context immediately. A ready
    message ensures both streams exist before the child sends `Stop`
    and closes IPC; the caller then waits for the missing result.

    A bounded TransportClosed exit proves fault-driven teardown, and
    reaped_children checks process completion and PID disappearance
    synchronously. Timer-driven cancellation cannot pass this test.

    '''
    async def main():
        '''
        Await transport failure and bound the entire nursery exit.

        '''
        with trio.fail_after(10):
            async with tractor.open_nursery(
                registry_addrs=[reg_addr],
                ensure_registry=True,
                enable_transports=[tpt_proto],
                start_method=spawn_backend,
                loglevel=loglevel,
            ) as an:
                portal = await an.start_actor(
                    'ipc_breaker',
                    enable_modules=[__name__],
                )
                async with portal.open_context(
                    break_ipc_after_started,
                ) as (ctx, sent):
                    async with ctx.open_stream() as stream:
                        await stream.send('ready')
                        await stream.receive()
                    await ctx.wait_for_result()
                    pytest.fail('broken context returned a result')

    with pytest.raises((TransportClosed, ExceptionGroup)) as err:
        trio.run(main)
    faults = assert_transport_closed(err.value, local=False)
    ctx = fault_contexts[0]
    assert any(fault is ctx.maybe_error for fault in faults)
    assert len(reaped_children) == len(fault_contexts) == 1
