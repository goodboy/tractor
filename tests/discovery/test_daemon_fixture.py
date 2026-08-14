'''
Discovery daemon fixture regressions.

'''
from unittest.mock import (
    call,
    Mock,
)

from .conftest import _wait_for_daemon_ready


def test_daemon_ready_check_does_not_connect(
    monkeypatch,
    tmp_path,
):
    '''
    Observe completed daemon startup without a raw connection.

    The old UDS readiness helper connected and immediately closed. That
    entered Tractor's actor-handshake handler with no `Aid` payload and
    destabilized the remote registrar on macOS before discovery tests
    started. This test creates the child sentinel, forbids all socket
    construction and connection helpers, then proves readiness returns
    without touching the transport layer.

    '''
    ready_path = tmp_path / 'daemon-ready'
    ready_path.touch()
    socket_ctor = Mock(side_effect=AssertionError('socket opened'))
    connect = Mock(side_effect=AssertionError('socket connected'))
    monkeypatch.setattr('socket.socket', socket_ctor)
    monkeypatch.setattr('socket.create_connection', connect)

    _wait_for_daemon_ready(
        ready_path=ready_path,
        deadline=.1,
        poll_interval=.01,
    )

    socket_ctor.assert_not_called()
    connect.assert_not_called()


def test_daemon_ready_check_backs_off(monkeypatch):
    '''
    Back off while waiting for the child startup sentinel.

    The sentinel may appear after several parent polling intervals.
    A deterministic false/false/true path sequence proves the helper
    sleeps between unsuccessful observations instead of hot-spinning
    and starving a booting daemon on constrained CI workers.

    '''
    ready_path = Mock()
    ready_path.is_file.side_effect = [False, False, True]
    sleep = Mock()
    monotonic = Mock(side_effect=[0, 0, 0, 0])
    monkeypatch.setattr('time.sleep', sleep)
    monkeypatch.setattr('time.monotonic', monotonic)

    _wait_for_daemon_ready(
        ready_path=ready_path,
        deadline=.2,
        poll_interval=.01,
    )

    assert ready_path.is_file.call_count == 3
    assert sleep.call_args_list == [
        call(.01),
        call(.01),
    ]
