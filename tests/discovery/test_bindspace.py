'''
Bindspace declaration, identity and live-capability contracts.

'''
from __future__ import annotations

from pathlib import Path
import os
import sys
from typing import BinaryIO

import msgspec
import pytest
import trio

from tractor.discovery import (
    BindspaceHandle,
    BindspaceIdentity,
    BindspaceOwnership,
    BindspaceSpec,
    CURRENT_NETNS,
    attach_netns,
    open_bindspace,
    open_netns,
)
from tractor.discovery import _bindspace
from tractor.msg import ProcessLocal


def test_bindspace_declarations_roundtrip() -> None:
    '''
    Spawn configuration and realized identity must cross actor IPC.

    Encode both frozen structs through msgpack and decode with their
    concrete types, proving names and stable inode identity survive
    without carrying any process-local capability state.

    '''
    values: tuple[
        BindspaceSpec|BindspaceIdentity,
        ...,
    ] = (
        BindspaceSpec(
            kind='netns',
            key='tractor-wg0',
            lifecycle='open',
        ),
        BindspaceIdentity(
            kind='netns',
            key='tractor-wg0',
            inode=1234,
        ),
    )
    value: BindspaceSpec|BindspaceIdentity
    for value in values:
        encoded: bytes = msgspec.msgpack.encode(value)
        decoded: BindspaceSpec|BindspaceIdentity = (
            msgspec.msgpack.decode(
                encoded,
                type=type(value),
            )
        )
        assert decoded == value


def test_bindspace_handle_pins_local_capability(
    tmp_path: Path,
) -> None:
    '''
    A live handle pins one exact FD and realized identity.

    Open a stand-in platform handle, record its inode in the realized
    identity and construct an owned capability. Prove the generic
    msgspec struct retains that exact local state. Its ability to
    encode ordinary fields is not authority to transfer the handle.

    '''
    token_path: Path = tmp_path / 'bindspace'
    token_path.touch()
    namespace_file: BinaryIO
    with token_path.open('rb') as namespace_file:
        namespace_fd: int = namespace_file.fileno()
        inode: int = token_path.stat().st_ino
        spec: BindspaceSpec = BindspaceSpec(
            kind='netns',
            key='tractor-wg0',
            lifecycle='open',
        )
        identity: BindspaceIdentity = BindspaceIdentity(
            kind='netns',
            key='tractor-wg0',
            inode=inode,
        )
        handle: BindspaceHandle = BindspaceHandle(
            spec=spec,
            identity=identity,
            namespace_fd=namespace_fd,
            ownership='owned',
        )

        assert handle.spec is spec
        assert handle.identity is identity
        assert handle.namespace_fd == namespace_file.fileno()
        assert handle.ownership == 'owned'
        assert isinstance(handle, msgspec.Struct)
        assert isinstance(handle, ProcessLocal)
        with pytest.raises(
            TypeError,
            match='_ProcessLocalToken.*unsupported',
        ):
            msgspec.msgpack.encode(handle)


def test_bindspace_handle_rejects_mismatched_identity(
    tmp_path: Path,
) -> None:
    '''
    A name or inode mismatch would make a handle stale authority.

    Construct a requested named spec, then prove both a different
    realized name and an inode not belonging to the supplied FD are
    rejected before either can become a live capability.

    '''
    token_path: Path = tmp_path / 'bindspace'
    token_path.touch()
    spec: BindspaceSpec = BindspaceSpec(
        kind='netns',
        key='tractor-wg0',
    )
    # Keep ownership and FD fixed so only identity changes below.
    ownership: BindspaceOwnership = 'borrowed'
    namespace_file: BinaryIO
    with token_path.open('rb') as namespace_file:
        namespace_fd: int = namespace_file.fileno()
        wrong_name: BindspaceIdentity = BindspaceIdentity(
            kind='netns',
            key='other-wg',
            inode=token_path.stat().st_ino,
        )
        with pytest.raises(
            ValueError,
            match='Spec.key.*Identity.key',
        ):
            BindspaceHandle(
                spec=spec,
                identity=wrong_name,
                namespace_fd=namespace_fd,
                ownership=ownership,
            )

        wrong_inode: BindspaceIdentity = BindspaceIdentity(
            kind='netns',
            key='tractor-wg0',
            inode=token_path.stat().st_ino + 1,
        )
        with pytest.raises(
            ValueError,
            match='FD inode.*identity inode',
        ):
            BindspaceHandle(
                spec=spec,
                identity=wrong_inode,
                namespace_fd=namespace_fd,
                ownership=ownership,
            )


@pytest.mark.parametrize(
    ('model', 'kwargs', 'match'),
    (
        pytest.param(
            BindspaceIdentity,
            {
                'kind': 'netns',
                'key': None,
                'inode': None,
            },
            'must be a positive `int`',
            id='identity-requires-inode',
        ),
        pytest.param(
            BindspaceSpec,
            {'kind': 'vrf'},
            'Unsupported bindspace kind',
            id='spec-rejects-kind',
        ),
        pytest.param(
            BindspaceIdentity,
            {
                'kind': 'vrf',
                'key': 'blue',
                'inode': 1234,
            },
            'Unsupported bindspace kind',
            id='identity-rejects-kind',
        ),
        pytest.param(
            BindspaceSpec,
            {
                'kind': 'netns',
                'key': '../outside',
            },
            'Invalid netns name',
            id='spec-rejects-path',
        ),
        pytest.param(
            BindspaceSpec,
            {
                'kind': 'netns',
                'key': '',
            },
            'BindspaceSpec.key',
            id='spec-rejects-empty-key',
        ),
        pytest.param(
            BindspaceSpec,
            {
                'kind': 'netns',
                'key': 'tractor-wg0',
                'lifecycle': 'replace',
            },
            'Unsupported bindspace lifecycle',
            id='spec-rejects-lifecycle',
        ),
        pytest.param(
            BindspaceIdentity,
            {
                'kind': 'netns',
                'key': '',
                'inode': 1234,
            },
            'BindspaceIdentity.key',
            id='identity-rejects-empty-key',
        ),
    ),
)
def test_bindspace_models_reject_invalid_values(
    model: type[BindspaceSpec]|type[BindspaceIdentity],
    kwargs: dict[str, object],
    match: str,
) -> None:
    '''
    Direct msgspec construction does not enforce field annotations.

    Parameterize the missing stable inode and future, unimplemented
    kinds. Prove neither serializable model can carry invalid
    identity or provisioning instructions into spawn configuration.

    '''
    with pytest.raises(ValueError, match=match):
        model(**kwargs)  # type: ignore[arg-type]


@pytest.mark.skipif(
    sys.platform != 'linux',
    reason='Linux netns API',
)
def test_open_bindspace_attaches_current_netns() -> None:
    '''
    The unnamed spec must borrow and pin the caller's current netns.

    Open `/proc/self/ns/net`, prove the yielded handle records its
    stable inode and borrowed ownership, then exit the context and
    prove the exact descriptor was closed without altering the
    namespace itself.

    '''
    async def main() -> int:
        '''
        Borrow the current netns and return its descriptor number.

        '''
        spec: BindspaceSpec = BindspaceSpec(
            kind='netns',
        )
        assert spec.key is CURRENT_NETNS
        async with open_bindspace(spec) as handle:
            namespace_fd: int|None = handle.namespace_fd
            assert namespace_fd is not None
            assert handle.spec is spec
            assert handle.identity.key is None
            assert handle.identity.inode == os.fstat(
                namespace_fd
            ).st_ino
            assert handle.ownership == 'borrowed'
            return namespace_fd

    namespace_fd: int = trio.run(main)
    with pytest.raises(OSError):
        os.fstat(namespace_fd)


@pytest.mark.skipif(
    sys.platform != 'linux',
    reason='Linux netns API',
)
def test_attach_named_netns_uses_run_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    '''
    A named spec must resolve only beneath the configured netns dir.

    Replace the run directory with a temporary stand-in, borrow its
    named inode, and prove the context neither deletes the existing
    resource nor leaves its descriptor open after exit.

    '''
    netns_path: Path = tmp_path / 'tractor-wg0'
    netns_path.touch()
    monkeypatch.setattr(
        _bindspace,
        '_NETNS_RUN_DIR',
        tmp_path,
    )

    async def main() -> int:
        '''
        Borrow the named stand-in and return its descriptor number.

        '''
        spec: BindspaceSpec = BindspaceSpec(
            kind='netns',
            key='tractor-wg0',
        )
        async with attach_netns(spec) as handle:
            namespace_fd: int|None = handle.namespace_fd
            assert namespace_fd is not None
            assert handle.identity.key == 'tractor-wg0'
            assert handle.identity.inode == netns_path.stat().st_ino
            assert handle.ownership == 'borrowed'
            return namespace_fd

    namespace_fd: int = trio.run(main)
    assert netns_path.exists()
    with pytest.raises(OSError):
        os.fstat(namespace_fd)


@pytest.mark.skipif(
    sys.platform != 'linux',
    reason='Linux netns API',
)
def test_attach_named_netns_never_creates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    '''
    Borrow-only lookup must fail without creating a missing resource.

    Point the run directory at an empty location, request one named
    netns, and prove the open error propagates while no path appears.

    '''
    monkeypatch.setattr(
        _bindspace,
        '_NETNS_RUN_DIR',
        tmp_path,
    )
    missing_path: Path = tmp_path / 'missing'

    async def main() -> None:
        '''
        Attempt to borrow one absent named netns.

        '''
        spec: BindspaceSpec = BindspaceSpec(
            kind='netns',
            key='missing',
        )
        async with attach_netns(spec):
            raise AssertionError('Missing netns unexpectedly opened')

    with pytest.raises(FileNotFoundError):
        trio.run(main)
    assert not missing_path.exists()


@pytest.mark.skipif(
    sys.platform != 'linux',
    reason='Linux netns API',
)
def test_open_netns_owns_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    '''
    Successful creation must yield ownership and remove on exit.

    Fake pyroute2 creation with a named stand-in file, verify the
    yielded FD and identity while it exists, then prove FD closure
    precedes resource removal when the context exits.

    '''
    events: list[str] = []
    namespace_fds: list[int] = []
    netns_path: Path = tmp_path / 'tractor-wg0'

    def create(key: str) -> None:
        '''
        Create the named stand-in and record lifecycle order.

        '''
        assert key == 'tractor-wg0'
        netns_path.touch()
        events.append('create')

    def remove(key: str) -> None:
        '''
        Remove the stand-in after its FD has closed.

        '''
        assert key == 'tractor-wg0'
        events.append('fd-closed')
        with pytest.raises(OSError):
            os.fstat(namespace_fds[0])
        netns_path.unlink()
        events.append('remove')

    monkeypatch.setattr(
        _bindspace,
        '_NETNS_RUN_DIR',
        tmp_path,
    )
    monkeypatch.setattr(
        _bindspace,
        '_create_netns',
        create,
    )
    monkeypatch.setattr(
        _bindspace,
        '_remove_netns',
        remove,
    )

    async def main() -> None:
        '''
        Open the fake netns and publish its live descriptor number.

        '''
        spec: BindspaceSpec = BindspaceSpec(
            kind='netns',
            key='tractor-wg0',
            lifecycle='open',
        )
        async with open_bindspace(spec) as handle:
            fd: int|None = handle.namespace_fd
            assert fd is not None
            assert handle.ownership == 'owned'
            assert handle.identity.inode == os.fstat(fd).st_ino
            namespace_fds.append(fd)
            events.append('yield')

    trio.run(main)
    assert events == [
        'create',
        'yield',
        'fd-closed',
        'remove',
    ]
    assert not netns_path.exists()


@pytest.mark.skipif(
    sys.platform != 'linux',
    reason='Linux netns API',
)
def test_open_netns_shields_cancelled_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    '''
    Cancellation after creation must not leak an owned namespace.

    Cancel the caller inside the yielded context and checkpoint.
    Prove shielded teardown still removes the stand-in before
    cancellation leaves the enclosing scope.

    '''
    netns_path: Path = tmp_path / 'tractor-wg0'
    removed: list[str] = []

    def create(key: str) -> None:
        '''
        Create the named stand-in before cancellation.

        '''
        netns_path.touch()

    def remove(key: str) -> None:
        '''
        Remove the stand-in despite caller cancellation.

        '''
        netns_path.unlink()
        removed.append(key)

    monkeypatch.setattr(
        _bindspace,
        '_NETNS_RUN_DIR',
        tmp_path,
    )
    monkeypatch.setattr(
        _bindspace,
        '_create_netns',
        create,
    )
    monkeypatch.setattr(
        _bindspace,
        '_remove_netns',
        remove,
    )

    async def main() -> None:
        '''
        Cancel while borrowing the newly owned namespace.

        '''
        spec: BindspaceSpec = BindspaceSpec(
            kind='netns',
            key='tractor-wg0',
            lifecycle='open',
        )
        with trio.CancelScope() as scope:
            async with open_netns(spec):
                scope.cancel()
                await trio.sleep_forever()

    trio.run(main)
    assert removed == ['tractor-wg0']
    assert not netns_path.exists()


@pytest.mark.skipif(
    sys.platform != 'linux',
    reason='Linux netns API',
)
def test_open_netns_requires_name() -> None:
    '''
    Creation cannot target the caller's current netns.

    Pass `CURRENT_NETNS` and prove validation rejects it before any
    privileged pyroute2 operation can run.

    '''
    spec: BindspaceSpec = BindspaceSpec(
        kind='netns',
        key=CURRENT_NETNS,
        lifecycle='open',
    )

    async def main() -> None:
        '''
        Attempt to create the unnamed current namespace.

        '''
        async with open_netns(spec):
            raise AssertionError(
                'Current netns unexpectedly created'
            )

    with pytest.raises(
        ValueError,
        match='requires a named',
    ):
        trio.run(main)
