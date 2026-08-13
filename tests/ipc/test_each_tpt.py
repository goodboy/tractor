'''
Unit-ish tests for specific IPC transport protocol backends.

'''
from __future__ import annotations
import os
from pathlib import Path
import stat
import sys

import pytest
import trio
import tractor
from tractor import Actor
from tractor.discovery import _addr
from tractor.runtime import _state


@pytest.fixture
def bindspace_dir_str() -> str:

    from tractor.runtime._state import get_rt_dir
    rt_dir: Path = get_rt_dir()
    bs_dir: Path = rt_dir / 'doggy'
    bs_dir_str: str = str(bs_dir)
    assert not bs_dir.is_dir()

    yield bs_dir_str

    # delete it on suite teardown.
    # ?TODO? should we support this internally
    # or is leaking it ok?
    if bs_dir.is_dir():
        bs_dir.rmdir()


def test_macos_rt_dir_fits_uds_path_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    '''
    Keep the default Darwin UDS bindpath below its 104-byte limit.

    `platformdirs` normally places the runtime directory below the
    long `~/Library/Caches/TemporaryItems` path. Pytest also assigns
    a deeply nested temporary home, so appending a registry socket
    name made every macOS UDS listener fail with `AF_UNIX path too
    long`. This test simulates Darwin and an intentionally long
    platformdirs result, then proves `get_rt_dir()` uses the short
    system temporary directory and leaves room for the socket name.

    '''
    long_rt_dir: Path = tmp_path / ('long' * 30)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(
        'platformdirs.user_runtime_dir',
        lambda appname: str(long_rt_dir / appname),
    )
    monkeypatch.setattr(_state, '_DARWIN_TMPDIR', tmp_path)
    rt_dir: Path = _state.get_rt_dir()
    sockpath: Path = (
        Path('/tmp')
        / f'tractor-{os.getuid()}'
        / 'registry@1616.sock'
    )

    assert rt_dir == tmp_path / f'tractor-{os.getuid()}'
    assert len(os.fsencode(sockpath)) < 104
    assert stat.S_IMODE(rt_dir.stat().st_mode) == 0o700


def test_macos_rt_dir_rejects_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    '''
    Reject a pre-created symlink at the Darwin runtime path.

    Darwin uses the predictable `/tmp/tractor-<uid>` path to stay
    below its `AF_UNIX` limit. A hostile local user could otherwise
    point that path at a victim-owned directory and make
    `get_rt_dir()` chmod or place sockets in the symlink target. The
    test replaces `/tmp` with a controlled directory, installs the
    malicious link, and proves non-following validation rejects it.

    '''
    runtime_link: Path = tmp_path / f'tractor-{os.getuid()}'
    target_dir: Path = tmp_path / 'target'
    target_dir.mkdir(mode=0o755)
    runtime_link.symlink_to(target_dir, target_is_directory=True)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(_state, '_DARWIN_TMPDIR', tmp_path)

    with pytest.raises(PermissionError, match='Unsafe Darwin'):
        _state.get_rt_dir()

    assert stat.S_IMODE(target_dir.stat().st_mode) == 0o755


def test_uds_bindspace_created_implicitly(
    debug_mode: bool,
    bindspace_dir_str: str,
):
    registry_addr: tuple = (
        f'{bindspace_dir_str}',
        'registry@doggy.sock',
    )
    bs_dir_str: str = registry_addr[0]

    # XXX, ensure bindspace-dir DNE beforehand!
    assert not Path(bs_dir_str).is_dir()

    async def main():
        async with tractor.open_nursery(
            enable_transports=['uds'],
            registry_addrs=[registry_addr],
            debug_mode=debug_mode,
        ) as _an:

            # XXX MUST be created implicitly by
            # `.ipc._uds.start_listener()`!
            assert Path(bs_dir_str).is_dir()

            root: Actor = tractor.current_actor()
            assert root.is_registrar

            assert registry_addr in root.reg_addrs
            assert (
                registry_addr
                in
                _state._runtime_vars['_registry_addrs']
            )
            assert (
                _addr.wrap_address(registry_addr)
                in
                root.registry_addrs
            )

    trio.run(main)


def test_uds_double_listen_raises_connerr(
    debug_mode: bool,
    bindspace_dir_str: str,
):
    registry_addr: tuple = (
        f'{bindspace_dir_str}',
        'registry@doggy.sock',
    )

    async def main():
        async with tractor.open_nursery(
            enable_transports=['uds'],
            registry_addrs=[registry_addr],
            debug_mode=debug_mode,
        ) as _an:

            # runtime up
            root: Actor = tractor.current_actor()

            from tractor.ipc._uds import (
                start_listener,
                UDSAddress,
            )
            ya_bound_addr: UDSAddress = root.registry_addrs[0]
            try:
                await start_listener(
                    addr=ya_bound_addr,
                )
            except ConnectionError as connerr:
                assert type(src_exc := connerr.__context__) is OSError
                assert 'Address already in use' in src_exc.args
                # complete, exit test.

            else:
                pytest.fail('It dint raise a connerr !?')


    trio.run(main)
