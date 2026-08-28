'''
Pre-runtime Linux network-namespace entry validation.

'''
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

from tractor.spawn import _netns


def test_enter_netns_rejects_mismatched_inherited_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    A stale inherited FD must not enter a replacement namespace.

    Open a real stand-in FD, declare a different expected inode and
    replace `os.setns()` with a failure sentinel. The inode check must
    reject the capability before any irreversible namespace entry.

    '''
    token_path: Path = tmp_path / 'netns'
    token_path.touch()

    def fail_setns(namespace_fd: int, nstype: int) -> None:
        raise AssertionError('`setns()` must not be called')

    monkeypatch.setattr(_netns.os, 'setns', fail_setns)
    namespace_file: BinaryIO
    with token_path.open('rb') as namespace_file:
        inode: int = token_path.stat().st_ino
        with pytest.raises(
            ValueError,
            match=f'{inode}.*{inode + 1}',
        ):
            _netns.enter_netns(
                namespace_file.fileno(),
                # Deliberately differ from `token_path`'s inode.
                inode + 1,
            )


def test_enter_netns_verifies_post_entry_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Successful `setns()` is insufficient without post-entry proof.

    Use a real inherited FD and fake only the privileged syscall and
    `/proc/self/ns/net` observation. The recorded calls prove both
    hooks execute and `CLONE_NEWNET` constrains the namespace type;
    the returned inode proves bootstrap observed the expected netns.

    '''
    token_path: Path = tmp_path / 'netns'
    token_path.touch()
    setns_calls: list[tuple[int, int]] = []
    stat_calls: list[Path] = []

    def fake_setns(namespace_fd: int, nstype: int) -> None:
        setns_calls.append((namespace_fd, nstype))

    def fake_stat(path: Path) -> SimpleNamespace:
        stat_calls.append(path)
        return SimpleNamespace(st_ino=inode)

    namespace_file: BinaryIO
    with token_path.open('rb') as namespace_file:
        namespace_fd: int = namespace_file.fileno()
        inode: int = token_path.stat().st_ino
        monkeypatch.setattr(_netns.os, 'setns', fake_setns)
        monkeypatch.setattr(
            type(_netns._SELF_NETNS),
            'stat',
            fake_stat,
        )

        entered_inode: int = _netns.enter_netns(
            namespace_fd,
            inode,
        )

    assert setns_calls == [
        (namespace_fd, _netns.os.CLONE_NEWNET),
    ]
    assert stat_calls == [_netns._SELF_NETNS]
    assert entered_inode == inode


def test_enter_netns_rejects_wrong_post_entry_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Bootstrap must stop when the process lands in an unexpected netns.

    Let the inherited FD check and fake syscall succeed, then report a
    different `/proc/self/ns/net` inode. The post-entry guard must raise
    instead of allowing actor runtime sockets to start in the wrong
    namespace.

    '''
    token_path: Path = tmp_path / 'netns'
    token_path.touch()

    def fake_setns(namespace_fd: int, nstype: int) -> None:
        return None

    monkeypatch.setattr(
        _netns.os,
        'setns',
        fake_setns,
    )

    namespace_file: BinaryIO
    with token_path.open('rb') as namespace_file:
        inode: int = token_path.stat().st_ino

        def fake_stat(path: Path) -> SimpleNamespace:
            return SimpleNamespace(st_ino=inode + 1)

        monkeypatch.setattr(
            type(_netns._SELF_NETNS),
            'stat',
            fake_stat,
        )
        with pytest.raises(
            RuntimeError,
            match=f'{inode + 1}.*{inode}',
        ):
            _netns.enter_netns(
                namespace_file.fileno(),
                # Deliberately differ from `fake_stat()`'s inode + 1.
                inode,
            )
