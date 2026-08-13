'''
High-level `.ipc._server` unit tests.

'''
from __future__ import annotations
import errno

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
from tractor.ipc._transport import MsgpackTransport
# TODO, use/check-roundtripping with some of these wrapper types?
#
# from .._addr import Address
# from ._chan import Channel
# from ._transport import MsgTransport
# from ._uds import UDSAddress
# from ._tcp import TCPAddress


def test_send_normalizes_peer_reset():
    '''
    Normalize Darwin's pre-handshake peer reset as transport closure.

    A raw UDS readiness client connects and immediately disconnects.
    Darwin reports the server's first handshake write as
    `ECONNRESET`, wrapped by `trio.BrokenResourceError`; allowing
    that raw error to escape cancels the daemon's shared IPC nursery.
    This fake stream reproduces the exact exception chain and proves
    `.send()` raises the expected `TransportClosed` boundary instead.

    '''
    class ResetStream:
        async def send_all(self, data: bytes) -> None:
            try:
                raise OSError(
                    errno.ECONNRESET,
                    'Connection reset by peer',
                )
            except OSError as reset_err:
                raise trio.BrokenResourceError from reset_err

    async def main():
        transport = object.__new__(MsgpackTransport)
        transport.stream = ResetStream()
        transport._send_lock = trio.StrictFIFOLock()
        transport._laddr = 'local'
        transport._raddr = 'remote'
        transport._task = trio.lowlevel.current_task()

        with pytest.raises(TransportClosed) as exc_info:
            await transport.send(
                {'probe': True},
                strict_types=False,
            )

        assert exc_info.value.src_exc.__cause__.errno == (
            errno.ECONNRESET
        )

    trio.run(main)


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
