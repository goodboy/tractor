'''
High-level `.ipc._server` unit tests.

'''
from __future__ import annotations
import errno
from unittest.mock import (
    AsyncMock,
    Mock,
)

import msgspec
import pytest
import trio
from tractor import (
    devx,
    ipc,
    log,
)
from tractor._testing.addr import (
    get_rando_addr,
)
from tractor._exceptions import TransportClosed
from tractor.ipc._chan import Channel
from tractor.ipc import _server
from tractor.ipc._tcp import TCPAddress
from tractor.ipc._transport import MsgpackTransport
from tractor.msg.types import Aid
# TODO, use/check-roundtripping with some of these wrapper types?
#
# from .._addr import Address
# from ._chan import Channel
# from ._transport import MsgTransport
# from ._uds import UDSAddress
# from ._tcp import TCPAddress


def test_send_normalizes_only_grouped_peer_resets():
    '''
    Normalize only all-peer-close grouped transport failures.

    A UDS peer may disconnect before completing the actor handshake.
    Darwin can report the server's first handshake write as
    `ECONNRESET`, wrapped by `trio.BrokenResourceError` and potentially
    nested in an `ExceptionGroup`. This fake stream first groups reset
    and broken-pipe branches, proving `.send()` normalizes a complete
    peer-close tree to `TransportClosed`. It then groups a reset with
    an unrelated `ValueError`, proving the mixed failure remains a
    `trio.BrokenResourceError` instead of hiding the application error.

    '''
    def broken_resource(err_no: int) -> trio.BrokenResourceError:
        try:
            raise OSError(
                err_no,
                'Peer closed',
            )
        except OSError as peer_err:
            try:
                raise trio.BrokenResourceError from peer_err
            except trio.BrokenResourceError as broken_err:
                return broken_err

    class GroupedFailureStream:
        def __init__(self, exceptions: list[Exception]) -> None:
            self.exceptions = exceptions

        async def send_all(self, data: bytes) -> None:
            grouped_err = ExceptionGroup(
                'concurrent send failures',
                self.exceptions,
            )
            raise trio.BrokenResourceError from grouped_err

    async def main():
        transport = object.__new__(MsgpackTransport)
        transport.stream = GroupedFailureStream([
            broken_resource(errno.ECONNRESET),
            broken_resource(errno.EPIPE),
        ])
        transport._send_lock = trio.StrictFIFOLock()
        transport._laddr = 'local'
        transport._raddr = 'remote'
        transport._task = trio.lowlevel.current_task()

        with pytest.raises(TransportClosed) as exc_info:
            await transport.send(
                {'probe': True},
                strict_types=False,
            )

        grouped_err = exc_info.value.src_exc.__cause__
        assert isinstance(grouped_err, ExceptionGroup)
        assert len(grouped_err.exceptions) == 2

        transport.stream = GroupedFailureStream([
            ValueError('unrelated failure'),
            broken_resource(errno.ECONNRESET),
        ])
        with pytest.raises(trio.BrokenResourceError) as exc_info:
            await transport.send(
                {'probe': True},
                strict_types=False,
            )

        grouped_err = exc_info.value.__cause__
        assert isinstance(grouped_err, ExceptionGroup)
        assert isinstance(grouped_err.exceptions[0], ValueError)

    trio.run(main)


def test_handshake_normalizes_decode_error():
    '''
    Keep malformed pre-handshake frames out of the service nursery.

    A non-msgpack peer can trigger `msgspec.DecodeError` before a
    remote `Aid` exists. Letting that decoder error escape the inbound
    handler cancels the actor's shared IPC nursery. This fake channel
    proves `_do_handshake()` presents only `TransportClosed` upward.

    '''
    chan = object.__new__(Channel)
    chan.send = AsyncMock()
    chan.recv = AsyncMock(
        side_effect=msgspec.DecodeError('malformed handshake'),
    )

    async def main():
        with pytest.raises(TransportClosed) as exc_info:
            await chan._do_handshake(
                aid=Aid(
                    name='local',
                    uuid='local-uuid',
                    pid=1234,
                ),
                timeout=.1,
            )

        assert isinstance(
            exc_info.value.src_exc,
            msgspec.DecodeError,
        )

    trio.run(main)


def test_server_uses_independent_handshake_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    '''
    Give ordinary actor handshakes a distinct, generous deadline.

    Registry probes use short retries, but ordinary portal and child
    connections do not retry. Applying the probe's one-second timeout
    in the server can terminate a valid delayed child and leave its
    parent blocked in `IPCServer.wait_for_peer()`. This handler fake
    proves the server uses its separate pre-registration budget.

    '''
    handshake = AsyncMock(
        side_effect=TransportClosed(message='stop after assertion'),
    )
    chan = Mock(_do_handshake=handshake)
    actor = Mock(
        aid=Aid(
            name='local',
            uuid='local-uuid',
            pid=1234,
        ),
    )
    monkeypatch.setattr(
        Channel,
        'from_stream',
        Mock(return_value=chan),
    )
    monkeypatch.setattr(
        _server._state,
        'current_actor',
        Mock(return_value=actor),
    )

    async def main():
        await _server.handle_stream_from_peer(
            stream=Mock(),
            server=Mock(),
        )

    trio.run(main)
    handshake.assert_awaited_once_with(
        aid=actor.aid,
        timeout=_server._PRE_REG_HANDSHAKE_TIMEOUT,
    )
    assert _server._PRE_REG_HANDSHAKE_TIMEOUT == 10


@pytest.mark.parametrize(
    '_tpt_proto',
    ['uds', 'tcp']
)
def test_basic_ipc_server(
    _tpt_proto: str,
    debug_mode: bool,
    loglevel: str,
):

    # so we see the socket-listener reporting on console
    log.get_console_log("INFO")

    rando_addr: tuple = get_rando_addr(
        tpt_proto=_tpt_proto,
    )
    async def main():
        async with ipc._server.open_ipc_server() as server:

            assert (
                server._parent_tn
                and
                server._parent_tn is server._stream_handler_tn
            )
            assert server._no_more_peers.is_set()

            eps: list[ipc._server.Endpoint] = await server.listen_on(
                accept_addrs=[rando_addr],
                stream_handler_nursery=None,
            )
            assert (
                len(eps) == 1
                and
                (ep := eps[0])._listener
                and
                not ep.peer_tpts
            )

            server._parent_tn.cancel_scope.cancel()

        # !TODO! actually make a bg-task connection from a client
        # using `ipc._chan._connect_chan()`

    with devx.maybe_open_crash_handler(
        pdb=debug_mode,
    ):
        trio.run(main)


@pytest.mark.parametrize(
    '_tpt_proto',
    ['uds', 'tcp']
)
def test_ep_addr_reconciled_from_sockname(
    _tpt_proto: str,
    debug_mode: bool,
):
    '''
    Guard `Endpoint.start_listener()`'s post-bind reconciliation of
    `.addr` against the listener's `socket.getsockname()`.

    For `tcp` that reconciliation is the ONLY way a kernel-assigned
    port (from a `port=0` bind) is ever learned, so it must keep
    firing; for `uds` the sock-file path must survive the
    round-trip through `.from_addr()` unchanged.

    Both are pinned here *before* the reconciliation gets gated on
    an `Address.rebind_from_sockname` opt-out (for backends whose
    `getsockname()` reports something other than what was bound).

    '''
    async def main():
        async with ipc._server.open_ipc_server() as server:

            accept_addr: tuple[str, int|str]
            match _tpt_proto:
                # XXX the whole point: ask the kernel to pick.
                case 'tcp':
                    accept_addr = (
                        TCPAddress.def_bindspace,
                        0,
                    )
                case 'uds':
                    accept_addr = get_rando_addr(
                        tpt_proto=_tpt_proto,
                    )

            eps: list[ipc._server.Endpoint] = await server.listen_on(
                accept_addrs=[accept_addr],
                stream_handler_nursery=None,
            )
            assert len(eps) == 1
            ep: ipc._server.Endpoint = eps[0]
            sockname = ep._listener.socket.getsockname()

            match _tpt_proto:
                case 'tcp':
                    # the bind req was for "any port"..
                    assert accept_addr[1] == 0
                    # ..and the ep learned the real one.
                    assert ep.addr._port != 0
                    assert ep.addr.unwrap() == tuple(sockname[:2])

                case 'uds':
                    # sock-file path is stable across the
                    # `.from_addr()` round-trip.
                    assert ep.addr.unwrap() == accept_addr
                    assert str(ep.addr.sockpath) == sockname

            server._parent_tn.cancel_scope.cancel()

    with devx.maybe_open_crash_handler(
        pdb=debug_mode,
    ):
        trio.run(main)
