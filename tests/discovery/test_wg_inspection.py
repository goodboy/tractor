'''
Read-only WireGuard netlink inspection tests.

'''
from __future__ import annotations

import threading
from typing import (
    Any,
    NoReturn,
)

import pytest
import trio

from tractor.discovery import (
    read_wg_peers,
    read_wg_pubkey,
)

pyroute2: Any = pytest.importorskip('pyroute2')


_PUBKEY: str = 'g3x7z0AdV1rM6UQU22CC7IL3/ivn4DzrE7ikDhCZ/Dc='
_PEER_1: str = '7PClzcj8o1yAjyPJb0zL2Gt0s2J7yZ6c0JXYqNBGr0E='
_PEER_2: str = 'H7bJbl1bpY7VzDlB5wI3KjA7JsiYoMWGDJd8dYgc5iw='


class Attrs:
    '''
    Minimal pyroute2 netlink-attribute message fake.

    '''
    def __init__(
        self,
        **attrs: Any,
    ) -> None:
        '''
        Store attributes for `.get_attr()` lookups.

        '''
        self._attrs: dict[str, Any] = attrs

    def get_attr(
        self,
        name: str,
    ) -> Any:
        '''
        Return the named fake netlink attribute.

        '''
        return self._attrs.get(name)


def test_read_wg_keys_in_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Pyroute2's synchronous `WireGuard` API owns a private asyncio
    loop. Running it in the Trio thread would either block Trio or
    introduce that foreign loop into the actor runtime.

    Replace `pyroute2.WireGuard` with a fake which records thread,
    iface, netns and close state. Return a multipart dump containing
    duplicate peers, then prove both public helpers execute
    off-thread, preserve named-netns selection, validate keys,
    deduplicate peers in kernel order and close every netlink client.

    '''
    trio_thread: int = threading.get_ident()

    class FakeWireGuard:
        '''
        Record each read-only `pyroute2.WireGuard` interaction.

        '''
        def __init__(
            self,
            *,
            netns: str|None,
            flags: int,
        ) -> None:
            '''
            Record namespace selection without opening netlink.

            '''
            self.netns = netns
            self.flags = flags
            self.closed = False
            self.thread_id: int|None = None
            self.iface: str|None = None
            instances.append(self)

        def info(
            self,
            iface: str,
        ) -> tuple[Attrs, Attrs]:
            '''
            Return a multipart WireGuard device dump.

            '''
            self.thread_id = threading.get_ident()
            self.iface = iface
            peer_1: Attrs = Attrs(
                WGPEER_A_PUBLIC_KEY=_PEER_1.encode(),
            )
            peer_2: Attrs = Attrs(
                WGPEER_A_PUBLIC_KEY=_PEER_2.encode(),
            )
            return (
                Attrs(
                    WGDEVICE_A_PUBLIC_KEY=_PUBKEY.encode(),
                    WGDEVICE_A_PEERS=[peer_1],
                ),
                Attrs(
                    WGDEVICE_A_PUBLIC_KEY=_PUBKEY.encode(),
                    WGDEVICE_A_PEERS=[peer_2, peer_1],
                ),
            )

        def close(self) -> None:
            '''
            Record netlink-client cleanup.

            '''
            self.closed = True

    instances: list[FakeWireGuard] = []
    monkeypatch.setattr(
        pyroute2,
        'WireGuard',
        FakeWireGuard,
    )

    async def main() -> None:
        '''
        Read both key views from Trio's run thread.

        '''
        assert await read_wg_pubkey(
            iface='wg-test',
            netns='actor-net',
        ) == _PUBKEY
        assert await read_wg_peers(
            iface='wg-test',
            netns='actor-net',
        ) == (_PEER_1, _PEER_2)

    trio.run(main)

    assert len(instances) == 2
    instance: FakeWireGuard
    for instance in instances:
        assert instance.netns == 'actor-net'
        assert instance.flags == 0
        assert instance.iface == 'wg-test'
        assert instance.thread_id != trio_thread
        assert instance.closed


def test_wg_client_closes_when_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    A failed netlink read must not leak pyroute2's socket or private
    event loop. Raise from the fake `.info()` call and prove the same
    error reaches the Trio caller only after `.close()` runs.

    '''
    class FakeWireGuard:
        '''
        Raise during device inspection and record cleanup.

        '''
        def __init__(
            self,
            *,
            netns: str|None,
            flags: int,
        ) -> None:
            '''
            Publish this fake instance for the cleanup assertion.

            '''
            nonlocal instance
            self.closed = False
            instance = self

        def info(self, iface: str) -> NoReturn:
            '''
            Simulate a failing netlink device read.

            '''
            raise OSError('netlink read failed')

        def close(self) -> None:
            '''
            Record cleanup after the failed read.

            '''
            self.closed = True

    instance: FakeWireGuard|None = None
    monkeypatch.setattr(
        pyroute2,
        'WireGuard',
        FakeWireGuard,
    )

    with pytest.raises(
        OSError,
        match='netlink read failed',
    ):
        trio.run(read_wg_pubkey)

    assert instance is not None
    assert instance.closed
