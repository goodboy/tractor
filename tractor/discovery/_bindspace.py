# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public License
# as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
'''
Serializable bindspace declarations and live capability handles.

'''
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager as acm
import os
from pathlib import Path
import sys
from typing import (
    Final,
    get_args,
    Literal,
    TypeAlias,
)

import msgspec
import trio

from ..msg._local import ProcessLocal


BindspaceKind: TypeAlias = Literal[
    'netns',
]
BindspaceOwnership: TypeAlias = Literal[
    'owned',  # manager tears down the resource after final release
    'borrowed',  # manager leaves the pre-existing resource intact
]

_NETNS_RUN_DIR: Path = Path('/var/run/netns')
_SELF_NETNS: Path = Path('/proc/self/ns/net')

CURRENT_NETNS: Final[None] = None


def _validate_bindspace_kind(
    kind: BindspaceKind,
) -> None:
    '''
    Reject platform-resource kinds without an implementation.

    '''
    if kind not in get_args(BindspaceKind):
        raise ValueError(
            f'Unsupported bindspace kind: {kind!r}'
        )


def _validate_bindspace_key(
    kind: BindspaceKind,
    key: str|None,
    field: str,
) -> None:
    '''
    Reject empty or path-like platform-resource names.

    `None` is valid. Spell it `CURRENT_NETNS` for
    `BindspaceSpec.key`; `BindspaceIdentity.key = None` records an
    unnamed realized netns.

    '''
    if key == '':
        raise ValueError(
            f'`{field}` must be a non-empty name or `None` '
            f'(`CURRENT_NETNS` for `BindspaceSpec.key`)!'
        )
    if (
        kind == 'netns'
        and
        key is not None
        and
        (
            Path(key).name != key
            or
            key in ('.', '..')
        )
    ):
        raise ValueError(
            f'Invalid netns name: {key!r}'
        )


class BindspaceSpec(
    msgspec.Struct,
    frozen=True,
):
    '''
    Serializable declaration of one requested bindspace.

    For a netns spec, `.key = CURRENT_NETNS` selects the calling
    process's current namespace without a named-path lookup.

    '''
    kind: BindspaceKind
    key: str|None = CURRENT_NETNS

    def __post_init__(self) -> None:
        '''
        Reject an empty platform-resource key.

        '''
        _validate_bindspace_kind(self.kind)
        _validate_bindspace_key(
            self.kind,
            self.key,
            'BindspaceSpec.key',
        )


class BindspaceIdentity(
    msgspec.Struct,
    frozen=True,
):
    '''
    Serializable stable identity of one realized bindspace.

    `.key` is an optional, mutable namespace name. `.inode` is the
    required kernel identity which remains stable after rename or
    unlink.

    '''
    kind: BindspaceKind
    key: str|None
    inode: int

    def __post_init__(self) -> None:
        '''
        Require a stable platform identity and an optional name.

        '''
        _validate_bindspace_kind(self.kind)
        _validate_bindspace_key(
            self.kind,
            self.key,
            'BindspaceIdentity.key',
        )
        if (
            type(self.inode) is not int
            or
            self.inode <= 0
        ):
            raise ValueError(
                '`BindspaceIdentity.inode` must be a positive `int`!'
            )


class BindspaceHandle(
    ProcessLocal,
):
    '''
    Process-local capability for one live realized bindspace.

    `ProcessLocal` provides compact typed storage plus a default
    wire-encoding guard. Explicit FD transfer and handle construction
    belong to the supervisor's spawn/bootstrap path.

    '''
    spec: BindspaceSpec
    identity: BindspaceIdentity
    namespace_fd: int|None
    ownership: BindspaceOwnership

    def __post_init__(self) -> None:
        '''
        Validate and retain one scoped bindspace capability.

        '''
        spec: BindspaceSpec = self.spec
        identity: BindspaceIdentity = self.identity
        namespace_fd: int|None = self.namespace_fd
        ownership: BindspaceOwnership = self.ownership

        if spec.kind != identity.kind:
            raise ValueError(
                '`BindspaceSpec.kind` does not match '
                '`BindspaceIdentity.kind`!'
            )
        if (
            spec.key is not None
            and
            spec.key != identity.key
        ):
            raise ValueError(
                '`BindspaceSpec.key` does not match '
                '`BindspaceIdentity.key`!'
            )
        if ownership not in get_args(BindspaceOwnership):
            raise ValueError(
                f'Invalid bindspace ownership: {ownership!r}'
            )
        if namespace_fd is not None:
            if (
                type(namespace_fd) is not int
                or
                namespace_fd < 0
            ):
                raise ValueError(
                    '`namespace_fd` must be non-negative or `None`!'
                )
            fd_inode: int = os.fstat(namespace_fd).st_ino
            if identity.inode != fd_inode:
                raise ValueError(
                    f'Namespace FD inode {fd_inode} does not match '
                    f'identity inode {identity.inode}!'
                )

    def __repr__(self) -> str:
        '''
        Render capability identity without dereferencing its FD.

        '''
        return (
            f'{type(self).__name__}('
            f'identity={self.identity!r}, '
            f'ownership={self.ownership!r}, '
            f'namespace_fd={self.namespace_fd!r})'
        )


@acm
async def attach_netns(
    spec: BindspaceSpec,
) -> AsyncIterator[BindspaceHandle]:
    '''
    Borrow and pin one existing Linux network namespace.

    `BindspaceSpec.key = CURRENT_NETNS` selects the calling process's
    current netns. A named key resolves beneath the standard iproute2
    netns run directory. "Attach" pins an existing namespace FD; this
    context never calls `setns()` or creates/removes a namespace.

    '''
    if sys.platform != 'linux':
        raise NotImplementedError(
            'Network namespace bindspaces are Linux-only!'
        )

    key: str|None = spec.key
    namespace_path: Path = (
        _SELF_NETNS
        if key is CURRENT_NETNS
        else _NETNS_RUN_DIR / key
    )
    namespace_fd: int = os.open(
        namespace_path,
        os.O_RDONLY | os.O_CLOEXEC,
    )
    try:
        inode: int = os.fstat(namespace_fd).st_ino
        identity: BindspaceIdentity = BindspaceIdentity(
            kind='netns',
            key=key,
            inode=inode,
        )
        handle: BindspaceHandle = BindspaceHandle(
            spec=spec,
            identity=identity,
            namespace_fd=namespace_fd,
            ownership='borrowed',
        )
        yield handle
    finally:
        os.close(namespace_fd)


def _create_netns(
    key: str,
) -> None:
    '''
    Create one named netns through pyroute2's synchronous API.

    '''
    try:
        from pyroute2 import netns
    except ImportError as exc:
        raise RuntimeError(
            'Netns creation requires the `tractor[wg]` extra.'
        ) from exc

    netns.create(key)


def _remove_netns(
    key: str,
) -> None:
    '''
    Remove one named netns through pyroute2's synchronous API.

    '''
    try:
        from pyroute2 import netns
    except ImportError as exc:
        raise RuntimeError(
            'Netns removal requires the `tractor[wg]` extra.'
        ) from exc

    netns.remove(key)


@acm
async def open_netns(
    spec: BindspaceSpec,
) -> AsyncIterator[BindspaceHandle]:
    '''
    Create, pin and own one named Linux network namespace.

    Creation and removal are shielded synchronous pyroute2 calls in a
    worker thread. This context never enters the namespace.
    Spawn-time bootstrap remains responsible for eventual `setns()`.

    '''
    if sys.platform != 'linux':
        raise NotImplementedError(
            'Network namespace bindspaces are Linux-only!'
        )

    key: str|None = spec.key
    if key is CURRENT_NETNS:
        raise ValueError(
            '`open_netns()` requires a named `BindspaceSpec.key`!'
        )

    created: bool = False
    try:
        with trio.CancelScope(shield=True):
            await trio.to_thread.run_sync(
                _create_netns,
                key,
                abandon_on_cancel=False,
            )
            created = True

        async with attach_netns(spec) as borrowed:
            handle: BindspaceHandle = BindspaceHandle(
                spec=spec,
                identity=borrowed.identity,
                namespace_fd=borrowed.namespace_fd,
                ownership='owned',
            )
            yield handle
    finally:
        if created:
            with trio.CancelScope(shield=True):
                await trio.to_thread.run_sync(
                    _remove_netns,
                    key,
                    abandon_on_cancel=False,
                )
