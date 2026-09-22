'''
Context status ownership and public Trio cancellation reasons.

'''
from unittest.mock import Mock

import msgspec
import pytest
import trio

from tractor._context import (
    Context,
    ContextCancelReason,
    ContextStatus,
    Unresolved,
)
from tractor._exceptions import (
    ContextCancelled,
    MsgTypeError,
    RemoteActorError,
    TransportClosed,
    _raise_from_unexpected_msg,
)
from tractor.msg import Error, Return, Started


LOCAL = ('local', 'actor-id')
PEER = ('peer', 'actor-id')


def make_context(**kwargs) -> Context:
    '''
    Supply inert actor resources around a real Context and channels.

    '''
    send, recv = trio.open_memory_channel(1)
    ctx = Context(
        Mock(aid=Mock(uid=PEER)),
        'context-id',
        Mock(aid=Mock(uid=LOCAL)),
        recv,
        send,
        Mock(),
        'test:context',
        **kwargs,
    )
    ctx.pformat = lambda **kw: 'test context'
    return ctx


def boxed_error(error_type, **kwargs):
    '''
    Construct real exception metadata without an actor process tree.

    '''
    return error_type(
        'fault',
        ipc_msg=Error(
            src_uid=PEER,
            src_type_str=error_type.__name__,
            boxed_type_str=error_type.__name__,
            relay_path=[PEER],
            cid='context-id',
            **kwargs,
        ),
    )


def test_status_initialization_and_isolation():
    '''
    The prototype duplicated ContextStatus fields on Context, so
    runtime writes never updated its status. Mutate one context's
    status and verify a second context retains all initial values.
    Public outcome and cancellation APIs must read the changed store
    while independent contexts remain unresolved and uncancelled.
    Diagnostics must expose state without including live resources.

    '''
    values = dict(
        _outcome_msg=Return(cid='context-id', pld=42),
        _result=42,
        _local_error=ValueError('local'),
        _remote_error=ValueError('remote'),
        _cancel_called=True,
        _canceller=LOCAL,
        _cancel_msg={'cancel': 'reason'},
        _started_called=True,
        _started_msg=Started(cid='context-id', pld='ready'),
        _started_pld=['ready'],
        _stream_opened=True,
    )
    first = make_context()
    other = make_context()
    assert first._status is not other._status
    assert set(values) == set(ContextStatus.__struct_fields__)
    for name, value in values.items():
        setattr(first._status, name, value)
    assert msgspec.structs.asdict(other._status) == {
        '_outcome_msg': Unresolved,
        '_result': Unresolved,
        '_local_error': None,
        '_remote_error': None,
        '_cancel_called': False,
        '_canceller': None,
        '_cancel_msg': None,
        '_started_called': False,
        '_started_msg': None,
        '_started_pld': None,
        '_stream_opened': False,
    }
    assert first.cancel_called
    assert first.canceller == LOCAL
    assert not first.cancel_acked
    assert not first.cancelled_caught
    assert first.maybe_error is values['_local_error']
    assert first.outcome is first.maybe_error
    assert first.has_outcome
    assert not other.cancel_called
    assert other.canceller is None
    assert not other.cancel_acked
    assert not other.cancelled_caught
    assert other.maybe_error is None
    assert other.outcome is Unresolved
    assert not other.has_outcome
    assert msgspec.structs.asdict(first._status) == values
    diagnostic = repr(first._status)
    assert str(first._status) == diagnostic
    indented = first._status.pformat(field_indent=4, indent=3)
    for name in values:
        assert f'\n       {name}=' in indented
    assert indented.endswith('\n   )')
    assert first._status.pformat(4, 3) == indented
    for name in values:
        assert f'{name}=' in diagnostic
    for name in (
        '_strict_started', '_cancel_on_msgerr',
        '_enter_debugger_on_cancel', '_stream', '_caller_info',
        '_scope', '_scope_tn', '_task',
    ):
        assert name not in ContextStatus.__struct_fields__
        assert f'{name}=' not in diagnostic
    assert first._scope_token is not other._scope_token


@pytest.mark.parametrize('value', [None, 0, False, 'result'])
def test_public_result_from_status(value):
    '''
    Moving Context._result must preserve resolved falsey payloads.
    Supply a completed parent-side status, then read its public
    outcome and await the result twice. Neither call may drain the
    empty feeder or mistake None, zero or False for Unresolved.
    A sibling context must retain its independent unresolved state.

    '''
    async def main():
        ctx = make_context(_portal=Mock())
        other = make_context()
        ctx._status._result = value
        ctx._status._outcome_msg = Return(
            cid=ctx.cid,
            pld=value,
        )
        assert ctx.has_outcome
        assert ctx.outcome is value
        assert ctx.maybe_error is None
        for _ in range(2):
            assert await ctx.wait_for_result() is value
        assert not other.has_outcome
        assert other.outcome is Unresolved

    trio.run(main)


@pytest.mark.parametrize('cancel_called', [False, True])
def test_transport_is_not_self_cancelled(cancel_called):
    '''
    A pending local cancel followed by raw TransportClosed used to
    read src_uid before checking for ContextCancelled and crashed.
    Feed that exact state into both classification entry points;
    transport observation must never acknowledge self-cancellation.

    '''
    ctx = make_context()
    ctx._status._cancel_called = cancel_called
    ctx._status._canceller = LOCAL
    ctx._status._remote_error = TransportClosed('EOF')
    assert not ctx.cancel_acked
    assert not ctx._is_self_cancelled(TransportClosed('other EOF'))


@pytest.mark.parametrize(
    'kind, origin',
    [
        ('transport', 'local_transport'),
        ('task', 'remote_task'),
        ('boxed_transport', 'remote_task'),
        ('cancel', 'remote_cancel'),
        ('protocol', 'local_protocol'),
        ('boxed_protocol', 'remote_task'),
    ],
)
def test_reason_delivery_and_first_fault(kind, origin):
    '''
    A cancel payload must describe the first cancelling fault, not
    the latest ContextStatus._remote_error. Deliver a fault, then
    another before checkpointing the real Trio scope. Inspect
    Cancelled.reason publicly to prove payload delivery, scope and
    actor identity, immutable metadata, and mutable error lifetime.
    Protocol cases deliberately use the same peer src_uid to ensure
    local observation is distinguished from a boxed remote fault.

    '''
    async def main():
        ctx = make_context()
        ctx._status._cancel_called = kind == 'transport'
        if kind == 'transport':
            error = TransportClosed('EOF')
        elif kind == 'cancel':
            error = boxed_error(ContextCancelled, canceller=PEER)
        elif 'protocol' in kind:
            error = boxed_error(MsgTypeError)
        else:
            error = boxed_error(RemoteActorError)
            if kind == 'boxed_transport':
                error._ipc_msg.boxed_type_str = 'TransportClosed'
        later = TransportClosed('later EOF')
        observed = []
        with trio.CancelScope() as scope:
            ctx._scope = scope
            ctx._maybe_cancel_and_set_remote_error(
                error,
                local_protocol=kind == 'protocol',
            )
            ctx._maybe_cancel_and_set_remote_error(later)
            for _ in range(2):
                try:
                    await trio.lowlevel.checkpoint()
                except trio.Cancelled as cancelled:
                    observed.append(cancelled.reason)
        reason = observed[0]
        assert isinstance(reason, ContextCancelReason)
        assert observed[1] is reason
        assert reason.error is error
        assert ctx._status._remote_error is later
        assert reason.cid == ctx.cid
        assert reason.actor_uid == LOCAL
        assert reason.peer_uid == PEER
        assert reason.scope_token is ctx._scope_token
        assert reason.scope_token is not scope
        assert reason.origin == origin
        assert isinstance(reason.message, str)
        for name in reason.__struct_fields__:
            with pytest.raises(AttributeError):
                setattr(reason, name, None)
        error.add_note('retained exception remains mutable')
        assert reason.error.__notes__ == [
            'retained exception remains mutable',
        ]

    trio.run(main)


@pytest.mark.parametrize('blocked_on_receive', [False, True])
def test_self_cancel_ack_scope_policy(blocked_on_receive):
    '''
    Moving cancellation state must preserve self-cancel ack policy.
    A real ContextCancelled names the local actor as canceller;
    simulate its owner blocked on the feeder or elsewhere. Only the
    latter needs scope interruption, with a remote_cancel reason;
    both paths must acknowledge the same request through status.

    '''
    async def main():
        ctx = make_context()
        ctx._status._cancel_called = True
        ctx._is_blocked_on_rx_chan = lambda: blocked_on_receive
        with trio.CancelScope() as scope:
            ctx._scope = scope
            error = boxed_error(ContextCancelled, canceller=LOCAL)
            ctx._maybe_cancel_and_set_remote_error(error)
            assert ctx.cancel_acked
            assert ctx._status._canceller == LOCAL
            assert scope.cancel_called is not blocked_on_receive
            if not blocked_on_receive:
                with pytest.raises(trio.Cancelled) as caught:
                    await trio.lowlevel.checkpoint()
                assert caught.value.reason.origin == 'remote_cancel'
                assert caught.value.reason.error is error

    trio.run(main)


def test_existing_cancel_reason_and_protocol_policy():
    '''
    Reason metadata must neither overwrite an already cancelled
    scope nor defeat Context._cancel_on_msgerr. Disable that policy
    and deliver a local MTE, then cancel explicitly and deliver EOF.
    Status must record each error, but the first actual cancellation
    payload must still reach the checkpoint unchanged.

    '''
    async def main():
        ctx = make_context(_cancel_on_msgerr=False)
        original_reason = object()
        with trio.CancelScope() as scope:
            ctx._scope = scope
            error = boxed_error(MsgTypeError)
            ctx._maybe_cancel_and_set_remote_error(
                error, local_protocol=True,
            )
            assert ctx._status._remote_error is error
            assert not scope.cancel_called
            scope.cancel(reason=original_reason)
            later = TransportClosed('EOF')
            ctx._maybe_cancel_and_set_remote_error(later)
            assert ctx._status._remote_error is later
            with pytest.raises(trio.Cancelled) as caught:
                await trio.lowlevel.checkpoint()
            assert caught.value.reason is original_reason

    trio.run(main)


@pytest.mark.parametrize('ancestor_first', [False, True])
def test_ancestor_reason_is_not_context_reason(ancestor_first):
    '''
    ContextStatus._remote_error cannot identify which scope supplied
    a Cancelled. Cancel nested real scopes in either order. Observe
    the first propagated reason at a checkpoint, then shield the
    inner scope. Trio 0.34 retains that first reason even after the
    shield changes. Assert the source distinction without private
    Trio attributes or assuming the latest fault wins.

    '''
    async def main():
        ctx = make_context()
        ancestor_reason = object()
        with trio.CancelScope() as ancestor:
            with trio.CancelScope() as scope:
                ctx._scope = scope
                if ancestor_first:
                    ancestor.cancel(reason=ancestor_reason)
                ctx._maybe_cancel_and_set_remote_error(
                    TransportClosed('EOF'),
                )
                if not ancestor_first:
                    ancestor.cancel(reason=ancestor_reason)
                with pytest.raises(trio.Cancelled) as caught:
                    await trio.lowlevel.checkpoint()
                reason = caught.value.reason
                if ancestor_first:
                    assert reason is ancestor_reason
                else:
                    assert isinstance(reason, ContextCancelReason)
                    assert reason.scope_token is ctx._scope_token
                scope.shield = True
                with pytest.raises(trio.Cancelled) as caught:
                    await trio.lowlevel.checkpoint()
                assert caught.value.reason is reason

    trio.run(main)


@pytest.mark.parametrize('local_protocol', [False, True])
def test_protocol_origin_at_raise_boundary(local_protocol):
    '''
    PldRx emulates Error with the peer src_uid for local validation
    failures, indistinguishable from remote MTE by uid alone. Call
    its real raising boundary with each source exception and inspect
    the ensuing cancellation. ValidationError must select local
    protocol origin; a received Error must remain remote task origin.

    '''
    async def main():
        ctx = make_context()
        error = boxed_error(MsgTypeError)
        src_err = (
            msgspec.ValidationError('invalid payload')
            if local_protocol else RuntimeError('received Error')
        )
        with trio.CancelScope() as scope:
            ctx._scope = scope
            with pytest.raises(MsgTypeError):
                _raise_from_unexpected_msg(
                    ctx, error._ipc_msg, src_err, Mock(),
                    expect_msg=Return,
                )
            with pytest.raises(trio.Cancelled) as caught:
                await trio.lowlevel.checkpoint()
            assert caught.value.reason.origin == (
                'local_protocol' if local_protocol
                else 'remote_task'
            )

    trio.run(main)
