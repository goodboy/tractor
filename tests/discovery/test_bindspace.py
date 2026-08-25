'''
Bindspace declaration, identity and live-capability contracts.

'''
from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

import msgspec
import pytest

from tractor.discovery import (
    BindspaceHandle,
    BindspaceIdentity,
    BindspaceOwnership,
    BindspaceSpec,
)
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
        BindspaceSpec(kind='netns', key='tractor-wg0'),
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
