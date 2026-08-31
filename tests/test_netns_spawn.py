'''
Pre-runtime Linux network-namespace entry validation.

'''
from __future__ import annotations

import errno
from functools import partial
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import (
    Any,
    BinaryIO,
)

import pytest
import trio

import tractor
from tractor import (
    _child,
    _root,
)
from tractor.devx import _proctitle
from tractor.net._bindspace import (
    Bindspace,
    BindspaceRef,
    BindspaceSpec,
)
from tractor.msg import Aid
from tractor.spawn import (
    _entry,
    _mp,
    _netns,
    _spawn,
    _trio,
)
from tractor.trionics import patches


_SELF_NETNS_PATH = Path('/proc/thread-self/ns/net')
_SELF_FD_DIR = Path('/proc/self/fd')
_linux_netns_only = pytest.mark.skipif(
    sys.platform != 'linux',
    reason='Linux network namespace API',
)


def _assert_fd_closed(namespace_fd: int) -> None:
    '''
    Assert that bootstrap consumed its child-owned descriptor.

    '''
    with pytest.raises(OSError) as exc_info:
        os.fstat(namespace_fd)

    assert exc_info.value.errno == errno.EBADF


def _fds_referencing(
    reference_fd: int,
) -> set[int]:
    '''
    Find this process's FDs for the same open kernel object.

    Snapshotting the matching descriptor numbers around a spawn lets
    the E2E test detect a leaked `os.dup()` entry without replacing
    `open_process()` or observing the child's descriptor table.

    '''
    reference_stat: os.stat_result = os.fstat(reference_fd)
    matching_fds: set[int] = set()
    fd_path: Path
    for fd_path in _SELF_FD_DIR.iterdir():
        try:
            open_fd: int = int(fd_path.name)
            open_stat: os.stat_result = os.fstat(open_fd)
        except (OSError, ValueError):
            continue

        if (
            open_stat.st_dev == reference_stat.st_dev
            and
            open_stat.st_ino == reference_stat.st_ino
        ):
            matching_fds.add(open_fd)

    return matching_fds


def _bindspace_for_fd(namespace_fd: int) -> Bindspace:
    '''
    Build one borrowed stand-in netns capability around a real FD.

    '''
    key: str = 'spawn-test-netns'
    inode: int = os.fstat(namespace_fd).st_ino
    return Bindspace(
        spec=BindspaceSpec(
            kind='netns',
            key=key,
        ),
        ref=BindspaceRef(
            kind='netns',
            key=key,
            inode=inode,
        ),
        namespace_fd=namespace_fd,
        ownership='borrowed',
    )


def _run_in_unshared_netns(
    test_name: str,
    reexec_var: str,
) -> bool:
    '''
    Re-exec one E2E test with disposable user and net namespaces.

    Return `True` in the outer pytest process after nested pytest
    succeeds. Return `False` inside that nested process so the caller
    performs the privileged namespace transitions itself.
    '''
    if os.environ.get(reexec_var) == '1':
        return False

    unshare_path: str|None = shutil.which('unshare')
    if unshare_path is None:
        pytest.skip('`unshare` is unavailable')

    # Give nested pytest `CAP_SYS_ADMIN` only inside a disposable
    # user namespace. Probe separately so hosts disabling
    # unprivileged user namespaces skip cleanly.
    probe = subprocess.run(
        [
            unshare_path,
            '--user',
            '--map-root-user',
            '--net',
            'true',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode:
        reason: str = probe.stderr.strip()
        pytest.skip(
            f'unprivileged user/net namespaces unavailable: '
            f'{reason}'
        )

    nested_env: dict[str, str] = dict(os.environ)
    nested_env[reexec_var] = '1'
    nested_env['VIRTUAL_ENV'] = sys.prefix
    nested_rt_dir: Path = Path(
        tempfile.mkdtemp(prefix='tne-')
    )
    nested_env['XDG_RUNTIME_DIR'] = str(nested_rt_dir)
    python_bin: str = str(Path(sys.executable).parent)
    nested_env['PATH'] = (
        python_bin
        + os.pathsep
        + nested_env['PATH']
    )
    test_id: str = f'tests/test_netns_spawn.py::{test_name}'
    try:
        subprocess.run(
            [
                unshare_path,
                '--user',
                '--map-root-user',
                '--net',
                sys.executable,
                '-m',
                'pytest',
                test_id,
                '--spawn-backend=trio',
                '--tpt-proto=uds',
                '-x',
                '--tb=short',
                '--no-header',
                '--timeout=30',
            ],
            env=nested_env,
            check=True,
        )
    finally:
        shutil.rmtree(nested_rt_dir)

    return True


class _MockIpcServer:
    '''
    Provide the peer-event state used by `trio_proc()` tests.

    '''
    def __init__(self) -> None:
        self._peer_connected: dict[
            tuple[str, str],
            trio.Event,
        ] = {}

    async def wait_for_peer(
        self,
        child_uid: tuple[str, str],
    ) -> tuple[trio.Event, object]:
        '''
        Model a child that dies or fails before its handshake.

        '''
        await trio.sleep_forever()


def _netns_bootstrap_from_cmd(
    command: list[str],
) -> tuple[int, int]:
    '''
    Parse the namespace tuple that the exec child would receive.

    '''
    arg_index: int = command.index('--netns_bootstrap')
    return _child.parse_netns_bootstrap(command[arg_index + 1])


class _SpawnTestNursery:
    '''
    Track provisional Trio child publication during transport tests.

    '''
    def __init__(self) -> None:
        self._actor = SimpleNamespace(
            ipc_server=_MockIpcServer(),
        )
        self._children: dict[
            tuple[str, str],
            tuple,
        ] = {}

    def _register_child(
        self,
        subactor: object,
        proc: object,
        portal: object|None,
    ) -> tuple[trio.Event, trio.Event, bool]:
        '''
        Publish one provisional child after its peer event exists.

        '''
        uid: tuple[str, str] = subactor.aid.uid
        assert uid in self._actor.ipc_server._peer_connected
        assert portal is None
        self._children[uid] = (subactor, proc, portal)
        return (trio.Event(), trio.Event(), False)


def _spawn_test_subactor(uid: tuple[str, str]) -> SimpleNamespace:
    '''
    Build the actor fields reached before a failed Trio handshake.

    '''
    return SimpleNamespace(
        aid=Aid(
            name=uid[0],
            uuid=uid[1],
        ),
        loglevel=None,
        pformat=lambda: uid[0],
    )


async def _report_child_netns(
    inherited_fd: int,
) -> tuple[int, int]:
    '''
    Report namespace and inherited-FD inodes from a spawned actor.

    `_consume_netns_bootstrap()` has already entered the target netns
    and closed its bootstrap FD before this RPC can run. The unrelated
    `inherited_fd` must remain open because the caller included it in
    `proc_kwargs['pass_fds']` before Trio appended the netns FD.

    '''
    child_netns_fd: int = os.open(
        _SELF_NETNS_PATH,
        os.O_RDONLY,
    )
    try:
        child_netns_inode: int = os.fstat(child_netns_fd).st_ino
        inherited_inode: int = os.fstat(inherited_fd).st_ino
        return child_netns_inode, inherited_inode
    finally:
        os.close(child_netns_fd)


@_linux_netns_only
def test_root_netns_same_namespace_skips_setns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Root entry into the current netns must not need privilege.

    Pin the real current namespace and arm `enter_netns()` as a
    failure sentinel. The context validates through a duplicate,
    yield the current inode without calling `setns()`, preserve the
    source capability, and close the duplicates on normal exit.
    '''
    namespace_fd: int = os.open(
        _SELF_NETNS_PATH,
        os.O_RDONLY,
    )
    bindspace: Bindspace = _bindspace_for_fd(namespace_fd)
    initial_fds: set[int] = _fds_referencing(namespace_fd)

    def fail_enter_netns(
        inherited_fd: int,
        inode: int,
    ) -> int:
        '''
        Reject a privileged transition for the already-current netns.

        '''
        raise AssertionError('same-netns entry called `setns()`')

    monkeypatch.setattr(_netns, 'enter_netns', fail_enter_netns)
    try:
        with _netns._enter_netns_temporarily(
            bindspace,
        ) as entered_inode:
            assert entered_inode == bindspace.ref.inode
            assert os.fstat(namespace_fd).st_ino == entered_inode
            # The target duplicate and original-netns snapshot both
            # reference the already-current namespace.
            assert len(_fds_referencing(namespace_fd)) == (
                len(initial_fds) + 2
            )

        assert _fds_referencing(namespace_fd) == initial_fds
    finally:
        os.close(namespace_fd)


@_linux_netns_only
def test_root_netns_restores_after_body_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    A root-body failure must restore netns before it escapes.

    Use distinct regular files as deterministic namespace stand-ins,
    replace only `enter_netns()`, and raise a unique error from the
    context body. The recorded transitions prove target entry then
    original restoration. FD snapshots prove neither temporary handle
    leaks, while the caller-owned target FD remains live.
    '''
    original_path: Path = tmp_path / 'original-netns'
    target_path: Path = tmp_path / 'target-netns'
    original_path.touch()
    target_path.touch()
    namespace_fd: int = os.open(target_path, os.O_RDONLY)
    bindspace: Bindspace = _bindspace_for_fd(namespace_fd)
    initial_fds: set[int] = _fds_referencing(namespace_fd)
    transitions: list[int] = []
    body_error = RuntimeError('root body failed')

    def fake_enter_netns(
        inherited_fd: int,
        inode: int,
    ) -> int:
        '''
        Record each verified target or restoration descriptor.

        '''
        assert os.fstat(inherited_fd).st_ino == inode
        transitions.append(inode)
        return inode

    monkeypatch.setattr(_netns, '_SELF_NETNS', original_path)
    monkeypatch.setattr(_netns, 'enter_netns', fake_enter_netns)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            with _netns._enter_netns_temporarily(bindspace):
                raise body_error

        assert exc_info.value is body_error
        # The fake records target entry before the body, then original
        # restoration during context exit.
        assert transitions == [
            bindspace.ref.inode,
            original_path.stat().st_ino,
        ]
        assert _fds_referencing(namespace_fd) == initial_fds
        assert os.fstat(namespace_fd).st_ino == bindspace.ref.inode
    finally:
        os.close(namespace_fd)


@_linux_netns_only
def test_root_netns_restores_on_trio_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Trio cancellation must not interrupt root-netns restoration.

    Use deterministic namespace stand-ins and cancel the task inside
    `_enter_root_bindspace()` immediately before an explicit Trio
    checkpoint. The enclosing `CancelScope` catches cancellation only
    after async-context exit. Two recorded sync transitions and exact
    FD state then prove restoration and close completed first.
    '''
    original_path: Path = tmp_path / 'cancel-original-netns'
    target_path: Path = tmp_path / 'cancel-target-netns'
    original_path.touch()
    target_path.touch()
    namespace_fd: int = os.open(target_path, os.O_RDONLY)
    bindspace: Bindspace = _bindspace_for_fd(namespace_fd)
    initial_fds: set[int] = _fds_referencing(namespace_fd)
    transitions: list[int] = []

    def fake_enter_netns(
        inherited_fd: int,
        inode: int,
    ) -> int:
        '''
        Record target entry and original-netns restoration.

        '''
        assert os.fstat(inherited_fd).st_ino == inode
        transitions.append(inode)
        return inode

    monkeypatch.setattr(_netns, '_SELF_NETNS', original_path)
    monkeypatch.setattr(_netns, 'enter_netns', fake_enter_netns)

    async def main() -> None:
        '''
        Deliver cancellation at a checkpoint inside the netns scope.

        '''
        with trio.CancelScope() as cancel_scope:
            async with _root._enter_root_bindspace(bindspace):
                cancel_scope.cancel()
                await trio.lowlevel.checkpoint()

        assert cancel_scope.cancelled_caught

    try:
        trio.run(main)
        # Target entry is recorded first; original-netns restoration
        # is recorded when `_enter_root_bindspace()` exits.
        assert transitions == [
            bindspace.ref.inode,
            original_path.stat().st_ino,
        ]
        assert _fds_referencing(namespace_fd) == initial_fds
        assert os.fstat(namespace_fd).st_ino == bindspace.ref.inode
    finally:
        os.close(namespace_fd)


@_linux_netns_only
@pytest.mark.parametrize('body_fails', (False, True))
def test_root_netns_restore_error_precedence(
    body_fails: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Netns restoration failure must not hide a root-body failure.

    Model target entry as successful and fail the second transition,
    which is restoration. The normal-body case must propagate that
    restoration error. The failing-body case must instead preserve
    its unique error and attach restoration failure as a note. In both
    schedules an exact target-FD snapshot proves cleanup still closes
    the context's duplicates.
    '''
    original_path: Path = tmp_path / 'failed-restore-original'
    target_path: Path = tmp_path / 'failed-restore-target'
    original_path.touch()
    target_path.touch()
    namespace_fd: int = os.open(target_path, os.O_RDONLY)
    bindspace: Bindspace = _bindspace_for_fd(namespace_fd)
    initial_fds: set[int] = _fds_referencing(namespace_fd)
    body_error = RuntimeError('root body failed first')
    restore_error = RuntimeError('root netns restore failed')
    transitions: int = 0

    def fail_restore(
        inherited_fd: int,
        inode: int,
    ) -> int:
        '''
        Enter the target once, then fail original-netns restoration.

        '''
        nonlocal transitions
        assert os.fstat(inherited_fd).st_ino == inode
        transitions += 1
        if transitions == 2:
            raise restore_error
        return inode

    monkeypatch.setattr(_netns, '_SELF_NETNS', original_path)
    monkeypatch.setattr(_netns, 'enter_netns', fail_restore)
    expected_error: RuntimeError = (
        body_error
        if body_fails
        else restore_error
    )
    try:
        with pytest.raises(RuntimeError) as exc_info:
            with _netns._enter_netns_temporarily(bindspace):
                if body_fails:
                    raise body_error

        assert exc_info.value is expected_error
        assert transitions == 2
        if body_fails:
            assert body_error.__notes__
            assert 'restore the original' in body_error.__notes__[0]
            assert repr(restore_error) in body_error.__notes__[0]
        assert _fds_referencing(namespace_fd) == initial_fds
    finally:
        os.close(namespace_fd)


@_linux_netns_only
def test_root_netns_requires_live_bindspace_fd() -> None:
    '''
    Root entry cannot use `BindspaceRef.inode` without a live FD.

    Construct a valid ref-only `Bindspace` and enter the real root
    namespace scope directly. The concrete live-FD error must occur
    before namespace capture, probes, sockets, or actor runtime work.
    '''
    key: str = 'missing-root-netns'
    bindspace = Bindspace(
        spec=BindspaceSpec(
            kind='netns',
            key=key,
        ),
        ref=BindspaceRef(
            kind='netns',
            key=key,
            inode=1,
        ),
        # A stored inode cannot authorize namespace entry.
        namespace_fd=None,
        ownership='borrowed',
    )

    with pytest.raises(
        ValueError,
        match='bindspace.namespace_fd.*live netns FD',
    ):
        # Scope entry must reject the missing live handle.
        with _netns._enter_netns_temporarily(bindspace):
            pytest.fail('root scope accepted a ref-only bindspace')


@_linux_netns_only
def test_root_netns_rejects_closed_bindspace_fd() -> None:
    '''
    A stale integer is not a live root-netns capability.

    Construct a `Bindspace` while its real current-netns FD is open,
    close that caller-owned descriptor, then attempt root entry. The
    concrete live-FD error proves `os.dup()` validates the descriptor
    at entry time instead of trusting construction-time metadata.
    '''
    namespace_fd: int = os.open(
        _SELF_NETNS_PATH,
        os.O_RDONLY,
    )
    bindspace: Bindspace = _bindspace_for_fd(namespace_fd)
    os.close(namespace_fd)

    with pytest.raises(
        ValueError,
        match='bindspace.namespace_fd.*live FD',
    ):
        with _netns._enter_netns_temporarily(bindspace):
            pytest.fail('root scope accepted a closed bindspace FD')


@_linux_netns_only
def test_bound_root_rejects_persistent_forkserver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    A persistent multiprocessing forkserver process can retain a
    previous root's netns.

    Select `mp_forkserver` before opening a later bound root, modeling
    reuse of the forkserver process which `multiprocessing` creates
    once and uses for later child starts. The root API must reject that
    backend before namespace entry or runtime startup, preventing
    default children from silently inheriting its stale namespace.

    '''
    namespace_fd: int = os.open(
        _SELF_NETNS_PATH,
        os.O_RDONLY,
    )
    bindspace: Bindspace = _bindspace_for_fd(namespace_fd)
    monkeypatch.setattr(
        _spawn,
        '_spawn_method',
        'mp_forkserver',
    )

    async def main() -> None:
        '''
        Reject the unsafe backend at root-context entry.

        '''
        with pytest.raises(
            NotImplementedError,
            match='persistent forkserver',
        ):
            async with tractor.open_root_actor(
                bindspace=bindspace,
            ):
                pytest.fail('bound root started under mp_forkserver')

    try:
        trio.run(main)
        assert os.fstat(namespace_fd).st_ino == bindspace.ref.inode
    finally:
        os.close(namespace_fd)


@_linux_netns_only
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


@_linux_netns_only
def test_enter_netns_verifies_post_entry_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Successful `setns()` is insufficient without post-entry proof.

    Use a real inherited FD and fake only the privileged syscall and
    `/proc/thread-self/ns/net` observation. The calls prove both
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


@_linux_netns_only
def test_enter_netns_rejects_wrong_post_entry_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Bootstrap must stop when the process lands in an unexpected netns.

    Let the inherited FD check and fake syscall succeed, then report a
    different `/proc/thread-self/ns/net` inode. The guard must raise
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


def test_trio_child_cli_forwards_netns_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    The exec child must retain one atomic FD-and-inode capability.

    Supply the tuple exactly as the Trio parent emits it and replace
    `_actor_child_main()` before any runtime work. The captured kwargs
    prove argparse does not split, reorder, or drop either value while
    forwarding the capability to the child-owned cleanup boundary.

    '''
    calls: list[dict[str, object]] = []
    uid: tuple[str, str] = ('cli-netns-child', 'test')
    parent_addr: tuple[str, int] = ('127.0.0.1', 1616)
    bootstrap: tuple[int, int] = (12, 3456)

    def fake_actor_child_main(**kwargs: object) -> None:
        '''
        Capture parsed child-bootstrap arguments without starting Trio.

        '''
        calls.append(kwargs)

    monkeypatch.setattr(
        _child,
        '_actor_child_main',
        fake_actor_child_main,
    )

    _child.main([
        '--uid',
        str(uid),
        '--parent_addr',
        str(parent_addr),
        '--netns_bootstrap',
        str(bootstrap),
    ])

    assert calls == [{
        'uid': uid,
        'loglevel': None,
        'parent_addr': parent_addr,
        'infect_asyncio': False,
        'spawn_method': 'trio',
        'netns_bootstrap': bootstrap,
    }]


def test_trio_spawn_requires_live_bindspace_fd() -> None:
    '''
    A `BindspaceRef` alone cannot let a child enter its namespace.

    Construct a valid `Bindspace` with its required identity metadata
    but no open namespace FD. Calling `trio_proc()` must fail before
    `open_process()` because an inode identifies a namespace but does
    not provide an open handle that the child can inherit.

    '''
    key: str = 'missing-spawn-netns'
    bindspace = Bindspace(
        spec=BindspaceSpec(
            kind='netns',
            key=key,
        ),
        # A realized bindspace always retains identity metadata; this
        # test isolates the missing live-FD condition.
        ref=BindspaceRef(
            kind='netns',
            key=key,
            inode=1,
        ),
        namespace_fd=None,
        ownership='borrowed',
    )
    uid: tuple[str, str] = ('missing-netns-fd', 'test')

    async def main() -> None:
        '''
        Reject the ref-only capability before `open_process()`.

        '''
        with pytest.raises(
            ValueError,
            match='bindspace.namespace_fd.*required',
        ):
            await _trio.trio_proc(
                name=uid[0],
                actor_nursery=_SpawnTestNursery(),
                subactor=_spawn_test_subactor(uid),
                errors={},
                bind_addrs=[],
                parent_addr=('127.0.0.1', 1616),
                _runtime_vars={},
                bindspace=bindspace,
            )

    trio.run(main)


def test_trio_spawn_relays_bindspace_to_child_actor(
    tmp_path: Path,
    start_method: str,
    tpt_proto: str,
) -> None:
    '''
    Move a subactor into the relayed bindspace netns.

    Re-exec this one test inside an unprivileged user/net namespace,
    then move the nested pytest parent into a second netns. The actor
    initially inherits the second namespace but receives an FD for the
    first. UDS keeps the parent handshake reachable across the netns
    boundary. The child reports its resulting namespace inode and a
    caller-supplied inherited FD over a real `Portal`, proving the exec
    CLI, merged `pass_fds`, `setns()`, handshake, and RPC path.

    '''
    if start_method != 'trio':
        pytest.skip('bindspace FD relay is implemented by Trio spawn')

    if _run_in_unshared_netns(
        test_name=(
            'test_trio_spawn_relays_bindspace_to_child_actor'
        ),
        reexec_var='TRACTOR_TEST_NETNS_E2E_REEXEC',
    ):
        return

    assert start_method == 'trio'
    assert tpt_proto == 'uds'

    # Namespace creation/realization is outside this transport slice:
    # production spawn accepts an already-open `namespace_fd`. Build
    # both disposable namespaces directly for this E2E boundary.
    target_netns_fd: int = os.open(
        _SELF_NETNS_PATH,
        os.O_RDONLY,
    )
    target_netns_inode: int = os.fstat(target_netns_fd).st_ino
    initial_target_fds: set[int] = _fds_referencing(
        target_netns_fd,
    )
    assert target_netns_fd in initial_target_fds

    # Move the parent to a second netns after retaining an FD for the
    # first. The child must use that FD to differ from its parent.
    os.unshare(os.CLONE_NEWNET)
    parent_netns_fd: int = os.open(
        _SELF_NETNS_PATH,
        os.O_RDONLY,
    )
    parent_netns_inode: int = os.fstat(parent_netns_fd).st_ino
    assert parent_netns_inode != target_netns_inode

    inherited_path: Path = tmp_path / 'caller-pass-fd'
    inherited_path.touch()
    inherited_fd: int = os.open(inherited_path, os.O_RDONLY)
    bindspace: Bindspace = _bindspace_for_fd(target_netns_fd)

    async def main() -> None:
        '''
        Start the child and receive its namespace observations by RPC.

        '''
        async with tractor.open_nursery() as actor_nursery:
            portal: tractor.Portal = await actor_nursery.start_actor(
                'netns-bootstrap-child',
                bindspace=bindspace,
                enable_modules=[__name__],
                proc_kwargs={
                    'pass_fds': (inherited_fd,),
                },
            )
            report: tuple[int, int] = await portal.run(
                _report_child_netns,
                inherited_fd=inherited_fd,
            )

            (
                child_netns_inode,
                inherited_inode,
            ) = report
            assert child_netns_inode == target_netns_inode
            assert child_netns_inode != parent_netns_inode
            assert inherited_inode == inherited_path.stat().st_ino
            await portal.cancel_actor()

    try:
        trio.run(main)
        # Any `os.dup(target_netns_fd)` entry made for child exec must
        # now be absent from the parent's descriptor table.
        assert _fds_referencing(target_netns_fd) == initial_target_fds
        # Both descriptors supplied by this parent remain open after
        # the child exits; only the temporary `os.dup()` FD is closed.
        assert os.fstat(target_netns_fd).st_ino == bindspace.ref.inode
        assert os.fstat(inherited_fd).st_ino == inherited_path.stat().st_ino
    finally:
        os.close(parent_netns_fd)
        os.close(target_netns_fd)
        os.close(inherited_fd)


def test_root_actor_enters_and_restores_bindspace(
    tpt_proto: str,
) -> None:
    '''
    `open_root_actor()` must enter its supplied networking bindspace.

    Re-exec under an unprivileged user/net namespace, retain that
    first netns as the target, then move nested pytest into a second.
    A real UDS root actor must run its body in the target inode and
    keep the source capability open. After full actor teardown, exact
    FD and inode assertions prove its duplicate did not leak and the
    caller thread returned to the second/original netns.
    '''
    if _run_in_unshared_netns(
        test_name='test_root_actor_enters_and_restores_bindspace',
        reexec_var='TRACTOR_TEST_ROOT_NETNS_E2E_REEXEC',
    ):
        return

    assert tpt_proto == 'uds'
    target_netns_fd: int = os.open(
        _SELF_NETNS_PATH,
        os.O_RDONLY,
    )
    target_netns_inode: int = os.fstat(target_netns_fd).st_ino
    reffed_tgt_fds: set[int] = _fds_referencing(
        target_netns_fd,
    )
    bindspace: Bindspace = _bindspace_for_fd(target_netns_fd)

    # Pin the first disposable netns through `target_netns_fd`, then
    # move the caller into a distinct second netns. This gives the root
    # one real target to enter and one real caller netns to restore.
    os.unshare(os.CLONE_NEWNET)
    original_netns_fd: int = os.open(
        _SELF_NETNS_PATH,
        os.O_RDONLY,
    )
    original_netns_inode: int = os.fstat(original_netns_fd).st_ino
    assert original_netns_inode != target_netns_inode

    async def main() -> None:
        '''
        Inspect the real root runtime inside the target netns.

        '''
        async with tractor.open_root_actor(
            bindspace=bindspace,
            enable_transports=['uds'],
        ):
            body_netns_inode: int = _SELF_NETNS_PATH.stat().st_ino
            assert body_netns_inode == target_netns_inode
            assert os.fstat(target_netns_fd).st_ino == (
                target_netns_inode
            )

    try:
        trio.run(main)
        assert _SELF_NETNS_PATH.stat().st_ino == original_netns_inode
        assert _fds_referencing(
            target_netns_fd,
        ) == reffed_tgt_fds
        assert os.fstat(target_netns_fd).st_ino == target_netns_inode
    finally:
        os.close(original_netns_fd)
        os.close(target_netns_fd)


def test_trio_spawn_failure_closes_child_netns_fd_in_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    A failed exec must close the child netns FD in the parent process.

    Raise one unique error from `open_process()` after capturing and
    validating the FD made by `os.dup(Bindspace.namespace_fd)`. Since
    exec fails, no child inherits it. The backend must close that
    parent descriptor, preserve the original error, leave the original
    bindspace FD open, and avoid removing a child record that was never
    added to `ActorNursery._children`.

    '''
    namespace_path: Path = tmp_path / 'failed-trio-bindspace'
    namespace_path.touch()
    namespace_fd: int = os.open(namespace_path, os.O_RDONLY)
    bindspace: Bindspace = _bindspace_for_fd(namespace_fd)
    uid: tuple[str, str] = ('failed-netns-exec', 'test')
    child_fds: list[int] = []
    open_error = OSError('could not exec child')

    async def fail_open_process(
        command: list[str],
        **kwargs: object,
    ) -> trio.Process:
        '''
        Fail after checking the child FD in `pass_fds` and the CLI.

        '''
        child_fd, expected_inode = _netns_bootstrap_from_cmd(command)
        assert kwargs['pass_fds'] == (child_fd,)
        assert os.fstat(child_fd).st_ino == expected_inode
        child_fds.append(child_fd)
        raise open_error

    monkeypatch.setattr(
        _trio.trio.lowlevel,
        'open_process',
        fail_open_process,
    )
    actor_nursery = _SpawnTestNursery()

    async def main() -> None:
        '''
        Exercise cleanup before Trio child publication.

        '''
        with pytest.raises(OSError) as exc_info:
            await _trio.trio_proc(
                name=uid[0],
                actor_nursery=actor_nursery,
                subactor=_spawn_test_subactor(uid),
                errors={},
                bind_addrs=[],
                parent_addr=('127.0.0.1', 1616),
                _runtime_vars={},
                bindspace=bindspace,
            )

        assert exc_info.value is open_error

    try:
        trio.run(main)
        assert len(child_fds) == 1
        _assert_fd_closed(child_fds[0])
        assert os.fstat(namespace_fd).st_ino == bindspace.ref.inode
        assert actor_nursery._children == {}
    finally:
        os.close(namespace_fd)


def test_trio_spawn_cancel_closes_child_netns_fd_in_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Cancellation during exec must close the parent-side child netns FD.

    Park `open_process()` after it receives the descriptor made by
    `os.dup(Bindspace.namespace_fd)`, then cancel the task running
    `trio_proc()`. The controlled event fixes the cancellation point
    inside the open call. Cleanup must close that descriptor in the
    parent while preserving the original bindspace FD; no process or
    `ActorNursery._children` entry exists to reap at this schedule.

    '''
    namespace_path: Path = tmp_path / 'cancelled-trio-bindspace'
    namespace_path.touch()
    namespace_fd: int = os.open(namespace_path, os.O_RDONLY)
    bindspace: Bindspace = _bindspace_for_fd(namespace_fd)
    uid: tuple[str, str] = ('cancelled-netns-exec', 'test')
    open_called = trio.Event()
    child_fds: list[int] = []

    async def park_open_process(
        command: list[str],
        **kwargs: object,
    ) -> trio.Process:
        '''
        Record the child FD, then signal that the open call is parked.

        '''
        child_fd, expected_inode = _netns_bootstrap_from_cmd(command)
        assert kwargs['pass_fds'] == (child_fd,)
        assert os.fstat(child_fd).st_ino == expected_inode
        child_fds.append(child_fd)
        open_called.set()
        try:
            await trio.sleep_forever()
        except trio.Cancelled:
            raise

    monkeypatch.setattr(
        _trio.trio.lowlevel,
        'open_process',
        park_open_process,
    )
    actor_nursery = _SpawnTestNursery()

    async def run_spawn() -> None:
        '''
        Keep cancellation propagation explicit at the backend task.

        '''
        try:
            await _trio.trio_proc(
                name=uid[0],
                actor_nursery=actor_nursery,
                subactor=_spawn_test_subactor(uid),
                errors={},
                bind_addrs=[],
                parent_addr=('127.0.0.1', 1616),
                _runtime_vars={},
                bindspace=bindspace,
            )
        except trio.Cancelled:
            raise

    async def main() -> None:
        '''
        Cancel only after `open_process()` owns the checkpoint.

        '''
        async with trio.open_nursery() as nursery:
            nursery.start_soon(run_spawn)
            await open_called.wait()
            nursery.cancel_scope.cancel()

    try:
        trio.run(main)
        assert len(child_fds) == 1
        _assert_fd_closed(child_fds[0])
        assert os.fstat(namespace_fd).st_ino == bindspace.ref.inode
        assert actor_nursery._children == {}
    finally:
        os.close(namespace_fd)


def test_mp_spawn_rejects_bindspace_transport(
    tmp_path: Path,
) -> None:
    '''
    Unimplemented MP FD transfer must fail before process creation.

    Supply one valid live bindspace directly to the multiprocessing
    backend. Until spawn/forkserver reduction gives the child exclusive
    descriptor ownership, both variants must raise the same actionable
    error instead of silently booting an actor in the parent's netns.

    '''
    namespace_path: Path = tmp_path / 'mp-bindspace'
    namespace_path.touch()
    namespace_fd: int = os.open(namespace_path, os.O_RDONLY)
    bindspace: Bindspace = _bindspace_for_fd(namespace_fd)

    async def main() -> None:
        '''
        Invoke the backend before any multiprocessing context access.

        '''
        with pytest.raises(
            NotImplementedError,
            match='multiprocessing spawn backends',
        ):
            await _mp.mp_proc(
                name='unsupported-netns-child',
                actor_nursery=None,  # type: ignore[arg-type]
                subactor=None,  # type: ignore[arg-type]
                errors={},
                bind_addrs=[],
                parent_addr=('127.0.0.1', 1616),
                _runtime_vars={},
                bindspace=bindspace,
            )

    try:
        trio.run(main)
        assert os.fstat(namespace_fd).st_ino == bindspace.ref.inode
    finally:
        os.close(namespace_fd)


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
