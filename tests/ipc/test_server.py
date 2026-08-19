'''
High-level `.ipc._server` unit tests.

'''
from __future__ import annotations

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
from tractor.ipc._tcp import TCPAddress
# TODO, use/check-roundtripping with some of these wrapper types?
#
# from .._addr import Address
# from ._chan import Channel
# from ._transport import MsgTransport
# from ._uds import UDSAddress
# from ._tcp import TCPAddress


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
