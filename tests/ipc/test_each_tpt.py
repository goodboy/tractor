'''
Unit-ish tests for specific IPC transport protocol backends.

'''
from __future__ import annotations
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_rt_dir_rejects_non_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    '''
    Preserve the non-Darwin runtime-directory type contract.

    Replacing `Path.is_dir()` with unguarded `lstat()` briefly made
    existing files look like valid runtime directories on Linux.
    This test points `platformdirs` at a regular file and proves
    `get_rt_dir()` rejects it during initialization.

    '''
    rt_file: Path = tmp_path / 'runtime-file'
    rt_file.touch()
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(
        'platformdirs.user_runtime_dir',
        lambda appname: str(rt_file),
    )

    with pytest.raises(FileExistsError):
        _state.get_rt_dir()

    new_rt_dir: Path = tmp_path / 'new-runtime-dir'
    monkeypatch.setattr(
        'platformdirs.user_runtime_dir',
        lambda appname: str(new_rt_dir),
    )
    assert _state.get_rt_dir() == new_rt_dir
    assert stat.S_IMODE(new_rt_dir.stat().st_mode) == 0o700


def test_macos_rt_dir_rejects_intermediate_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    '''
    Reject symlinks in nested Darwin runtime subdirectories.

    The earlier final-component check allowed `link/child` to follow
    an intermediate symlink and create `child` outside the secured
    runtime root. This test installs that link and proves traversal
    stops before anything is created in its target.

    '''
    rt_root: Path = tmp_path / f'tractor-{os.getuid()}'
    target_dir: Path = tmp_path / 'target'
    rt_root.mkdir(mode=0o700)
    target_dir.mkdir()
    (rt_root / 'link').symlink_to(
        target_dir,
        target_is_directory=True,
    )
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(_state, '_DARWIN_TMPDIR', tmp_path)

    with pytest.raises(PermissionError, match='Unsafe Darwin'):
        _state.get_rt_dir(subdir='link/child')

    assert not (target_dir / 'child').exists()


@pytest.mark.parametrize(
    ('platform_name', 'path_limit'),
    [
        ('darwin', 104),
        ('linux', 108),
    ],
)
def test_uds_sockname_compaction(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    path_limit: int,
):
    '''
    Keep generated actor sockets safe and below Darwin's byte limit.

    Actor names are unrestricted identity strings. A long, multibyte,
    or path-like name previously produced overlong or escaping socket
    paths. These cases prove `UDSAddress.get_sockname()` preserves a
    short legacy name, deterministically compacts unsafe names, keeps
    the reaper's `@pid.sock` suffix, and stays within Darwin's byte
    limit.

    '''
    from tractor.ipc._uds import UDSAddress

    bindspace: Path = Path('/tmp/tractor-501')
    pid: int = 12345
    from tractor.ipc import _uds

    monkeypatch.setattr(sys, 'platform', platform_name)
    monkeypatch.setattr(_uds, '_SUN_PATH_LIMIT', path_limit)

    short: Path = UDSAddress.get_sockname(
        name='worker',
        pid=pid,
        bindspace=bindspace,
    )
    long_name: str = 'actor-' + ('\u00e9' * 100)
    compact: Path = UDSAddress.get_sockname(
        name=long_name,
        pid=pid,
        bindspace=bindspace,
    )
    unsafe: Path = UDSAddress.get_sockname(
        name='../worker',
        pid=pid,
        bindspace=bindspace,
    )

    assert short == Path(f'worker@{pid}.sock')
    assert compact == UDSAddress.get_sockname(
        name=long_name,
        pid=pid,
        bindspace=bindspace,
    )
    assert compact.name.endswith(f'@{pid}.sock')
    assert unsafe.parent == Path('.')
    assert '..' not in unsafe.name
    assert len(os.fsencode(bindspace / compact)) < path_limit

    with pytest.raises(ValueError) as exc_info:
        UDSAddress.get_sockname(
            name=long_name,
            pid=pid,
            bindspace=Path('/tmp') / ('x' * 90),
        )

    errmsg: str = str(exc_info.value)
    assert 'leaves no room' in errmsg
    assert 'name was unsafe: False' in errmsg
    assert 'name was over budget: True' in errmsg
    assert f'AF_UNIX path limit: {path_limit}' in errmsg


def test_uds_reaper_ignores_unreconstructable_path(
    monkeypatch: pytest.MonkeyPatch,
):
    '''
    Keep post-kill UDS cleanup best-effort on path overflow.

    `unlink_uds_bind_addrs()` reconstructs a self-assigned socket from
    the dead actor's name and PID. An over-budget bindspace makes that
    naming helper raise before `os.unlink()`; propagating the error
    would replace the original supervision outcome after the child was
    already killed. This test forces overflow and proves cleanup skips
    reconstruction without attempting an unlink or raising.

    '''
    from tractor.ipc import _uds
    from tractor.spawn import _reap

    long_bindspace: Path = Path('/tmp') / ('x' * 120)
    proc = SimpleNamespace(pid=12345)
    subactor = SimpleNamespace(
        aid=SimpleNamespace(name='worker'),
    )
    unlink = Mock()
    monkeypatch.setattr(
        _uds.UDSAddress,
        'def_bindspace',
        long_bindspace,
    )
    monkeypatch.setattr(_reap.os, 'unlink', unlink)

    _reap.unlink_uds_bind_addrs(
        proc=proc,
        subactor=subactor,
    )

    unlink.assert_not_called()


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
