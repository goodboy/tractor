'''
Pre-runtime Linux network-namespace entry validation.

'''
from __future__ import annotations

import errno
from functools import partial
import os
from pathlib import Path
from types import SimpleNamespace
from typing import (
    Any,
    BinaryIO,
)

import pytest

from tractor import _child
from tractor.devx import _proctitle
from tractor.spawn import (
    _entry,
    _netns,
    _spawn,
)
from tractor.trionics import patches


def _assert_fd_closed(namespace_fd: int) -> None:
    '''
    Assert that bootstrap consumed its child-owned descriptor.

    '''
    with pytest.raises(OSError) as exc_info:
        os.fstat(namespace_fd)

    assert exc_info.value.errno == errno.EBADF


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


def test_empty_netns_bootstrap_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Ordinary child startup must not attempt namespace entry.

    Leave the optional capability unset and arm `enter_netns()` as a
    failure sentinel. The bootstrap boundary must return without any
    syscall or descriptor ownership work for existing spawn callers.

    '''
    def fail_enter_netns(namespace_fd: int, inode: int) -> int:
        '''
        Reject namespace entry without an explicit capability.

        '''
        raise AssertionError('empty bootstrap attempted netns entry')

    monkeypatch.setattr(_entry, 'enter_netns', fail_enter_netns)

    assert _entry._consume_netns_bootstrap(None) is None


@pytest.mark.parametrize('namespace_fd', (-1, True, '1'))
def test_invalid_netns_fd_is_never_closed(
    namespace_fd: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Invalid descriptor values must not reach the OS close boundary.

    Feed negative, boolean, and non-integer values through the atomic
    capability. Preserve the namespace primitive's validation error
    without letting `bool` alias stdout or allowing cleanup to mask the
    primary failure.

    '''
    entry_error = ValueError('invalid netns capability')

    def fail_enter_netns(namespace_fd: int, inode: int) -> int:
        '''
        Raise the primary namespace bootstrap error.

        '''
        raise entry_error

    def fail_close(inherited_fd: int) -> None:
        '''
        Reject cleanup for a value that cannot be an owned FD.

        '''
        raise AssertionError('invalid namespace FD reached close')

    monkeypatch.setattr(_entry, 'enter_netns', fail_enter_netns)
    monkeypatch.setattr(
        _entry,
        'os',
        SimpleNamespace(close=fail_close),
    )

    with pytest.raises(ValueError) as exc_info:
        _entry._consume_netns_bootstrap(
            (namespace_fd, 1),  # type: ignore[arg-type]
        )

    assert exc_info.value is entry_error


def test_netns_entry_error_survives_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Descriptor cleanup must not mask the primary bootstrap failure.

    Raise a unique entry error for an oversized positive integer whose
    cleanup also raises `OverflowError`. The entry error must escape
    with cleanup context attached instead of being replaced by the
    close failure.

    '''
    namespace_fd: int = 1 << 100
    entry_error = ValueError('invalid netns capability')

    def fail_enter_netns(inherited_fd: int, inode: int) -> int:
        '''
        Raise the primary namespace bootstrap error.

        '''
        assert inherited_fd == namespace_fd
        raise entry_error

    monkeypatch.setattr(_entry, 'enter_netns', fail_enter_netns)

    with pytest.raises(ValueError) as exc_info:
        _entry._consume_netns_bootstrap((namespace_fd, 1))

    assert exc_info.value is entry_error
    assert entry_error.__notes__
    assert 'close inherited namespace FD' in entry_error.__notes__[0]
    assert 'OverflowError' in entry_error.__notes__[0]


@pytest.mark.parametrize('backend', ('mp', 'trio'))
def test_child_entry_consumes_netns_before_runtime(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Child bootstrap must enter its netns before runtime side effects.

    Give each child entrypoint an exclusively owned stand-in FD. Fake
    only namespace entry and every later bootstrap boundary, requiring
    the FD to remain open during entry but be closed before actor state,
    logging, multiprocessing setup, frame hiding, or `trio.run()`.
    This proves verified entry and capability release are one
    synchronous prefix of both child startup paths.

    '''
    token_path: Path = tmp_path / f'{backend}-netns'
    token_path.touch()
    namespace_fd: int = os.open(token_path, os.O_RDONLY)
    expected_inode: int = os.fstat(namespace_fd).st_ino
    events: list[str] = []

    def fake_enter_netns(
        inherited_fd: int,
        inode: int,
    ) -> int:
        '''
        Record verified entry while the capability remains open.

        '''
        assert os.fstat(inherited_fd).st_ino == expected_inode
        assert inherited_fd == namespace_fd
        assert inode == expected_inode
        events.append('enter-netns')
        return inode

    def record(
        event: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        '''
        Record one post-entry operation after proving FD release.

        '''
        _assert_fd_closed(namespace_fd)
        events.append(event)

    class ActorSpy:
        '''
        Record multiprocessing actor-state initialization.

        '''
        loglevel = None
        uid = ('netns-child', 'test')
        _infected_aio = False

        def __setattr__(
            self,
            name: str,
            value: object,
        ) -> None:
            '''
            Observe the first multiprocessing entrypoint mutation.

            '''
            if name == '_forkserver_info':
                record('forkserver-info')
            object.__setattr__(self, name, value)

    class StateSpy:
        '''
        Record actor publication into runtime-global state.

        '''
        def __setattr__(
            self,
            name: str,
            value: object,
        ) -> None:
            '''
            Observe `_state._current_actor` publication.

            '''
            record('runtime-state')
            object.__setattr__(self, name, value)

    def fake_current_process() -> str:
        '''
        Return one display value for multiprocessing startup logging.

        '''
        return 'fake-child-process'

    def fake_start_method(start_method: str) -> SimpleNamespace:
        '''
        Record multiprocessing setup after namespace entry.

        '''
        record('start-method')
        return SimpleNamespace(
            current_process=fake_current_process,
        )

    def fake_actor(**kwargs: object) -> ActorSpy:
        '''
        Record Trio child actor construction after namespace entry.

        '''
        record('actor-construction')
        return ActorSpy()

    monkeypatch.setattr(_entry, 'enter_netns', fake_enter_netns)
    monkeypatch.setattr(_entry, '_state', StateSpy())
    monkeypatch.setattr(
        _entry._frame_stack,
        'hide_runtime_frames',
        partial(record, 'hide-frames'),
    )
    monkeypatch.setattr(
        _entry.trio,
        'run',
        partial(record, 'trio-run'),
    )
    monkeypatch.setattr(
        _entry,
        'log',
        SimpleNamespace(
            info=partial(record, 'log'),
            cancel=partial(record, 'log'),
            error=partial(record, 'log'),
        ),
    )
    monkeypatch.setattr(
        _spawn,
        'try_set_start_method',
        fake_start_method,
    )
    monkeypatch.setattr(
        patches,
        'apply_all',
        partial(record, 'trio-patches'),
    )
    monkeypatch.setattr(_child, 'Actor', fake_actor)
    monkeypatch.setattr(
        _proctitle,
        'set_actor_proctitle',
        partial(record, 'proctitle'),
    )
    monkeypatch.setattr(
        _child,
        '_trio_main',
        partial(record, 'trio-main'),
    )

    actor: Any = ActorSpy()
    bootstrap: tuple[int, int] = (
        namespace_fd,
        expected_inode,
    )
    if backend == 'mp':
        _entry._mp_main(
            actor,
            [],
            (None, None, None, None, None),
            'mp_spawn',
            netns_bootstrap=bootstrap,
        )
        first_runtime_event: str = 'forkserver-info'
        terminal_event: str = 'trio-run'
    else:
        _child._actor_child_main(
            uid=actor.uid,
            loglevel=actor.loglevel,
            parent_addr=None,
            infect_asyncio=False,
            netns_bootstrap=bootstrap,
        )
        first_runtime_event = 'trio-patches'
        terminal_event = 'trio-main'

    assert events[:2] == [
        'enter-netns',
        first_runtime_event,
    ]
    assert events.count(terminal_event) == 1
    _assert_fd_closed(namespace_fd)


@pytest.mark.parametrize('backend', ('mp', 'trio'))
def test_child_entry_failure_closes_netns_fd_before_runtime(
    backend: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Failed namespace entry must close its FD and abort child startup.

    Raise a unique error from the namespace boundary while a real
    stand-in FD is open. Arm each entrypoint's first later operation as
    a failure sentinel, then prove the original error escapes, the
    descriptor is closed, and no actor, multiprocessing, frame, or
    Trio runtime initialization begins.

    '''
    token_path: Path = tmp_path / f'{backend}-failed-netns'
    token_path.touch()
    namespace_fd: int = os.open(token_path, os.O_RDONLY)
    expected_inode: int = os.fstat(namespace_fd).st_ino
    entry_error = RuntimeError('netns entry failed')
    events: list[str] = []

    def fail_enter_netns(
        inherited_fd: int,
        inode: int,
    ) -> int:
        '''
        Fail entry while proving the child-owned FD is still open.

        '''
        assert os.fstat(inherited_fd).st_ino == expected_inode
        assert inherited_fd == namespace_fd
        assert inode == expected_inode
        events.append('enter-netns')
        raise entry_error

    def fail_after_entry(*args: object, **kwargs: object) -> None:
        '''
        Reject any runtime operation after failed namespace entry.

        '''
        raise AssertionError('child runtime started after netns failure')

    class ActorSpy:
        '''
        Reject multiprocessing actor-state initialization.

        '''
        loglevel = None
        uid = ('failed-netns-child', 'test')

        def __setattr__(
            self,
            name: str,
            value: object,
        ) -> None:
            '''
            Reject the first multiprocessing entrypoint mutation.

            '''
            if name == '_forkserver_info':
                fail_after_entry()
            object.__setattr__(self, name, value)

    monkeypatch.setattr(_entry, 'enter_netns', fail_enter_netns)
    monkeypatch.setattr(
        _entry._frame_stack,
        'hide_runtime_frames',
        fail_after_entry,
    )
    monkeypatch.setattr(
        _spawn,
        'try_set_start_method',
        fail_after_entry,
    )
    monkeypatch.setattr(
        patches,
        'apply_all',
        fail_after_entry,
    )
    monkeypatch.setattr(_child, 'Actor', fail_after_entry)
    monkeypatch.setattr(
        _proctitle,
        'set_actor_proctitle',
        fail_after_entry,
    )
    monkeypatch.setattr(
        _child,
        '_trio_main',
        fail_after_entry,
    )

    actor: Any = ActorSpy()
    bootstrap: tuple[int, int] = (
        namespace_fd,
        expected_inode,
    )
    with pytest.raises(RuntimeError) as exc_info:
        if backend == 'mp':
            _entry._mp_main(
                actor,
                [],
                (None, None, None, None, None),
                'mp_spawn',
                netns_bootstrap=bootstrap,
            )
        else:
            _child._actor_child_main(
                uid=actor.uid,
                loglevel=actor.loglevel,
                parent_addr=None,
                infect_asyncio=False,
                netns_bootstrap=bootstrap,
            )

    assert exc_info.value is entry_error
    assert events == ['enter-netns']
    _assert_fd_closed(namespace_fd)
