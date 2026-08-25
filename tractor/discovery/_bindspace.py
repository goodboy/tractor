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

import os
from typing import (
    get_args,
    Literal,
    TypeAlias,
)

import msgspec

from ..msg._local import ProcessLocal


BindspaceKind: TypeAlias = Literal[
    'netns',
]
BindspaceOwnership: TypeAlias = Literal[
    'owned',  # manager tears down the resource after final release
    'borrowed',  # manager leaves the pre-existing resource intact
]


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


class BindspaceSpec(
    msgspec.Struct,
    frozen=True,
):
    '''
    Serializable declaration of one requested bindspace.

    '''
    kind: BindspaceKind
    key: str|None = None

    def __post_init__(self) -> None:
        '''
        Reject an empty platform-resource key.

        '''
        _validate_bindspace_kind(self.kind)
        if self.key == '':
            raise ValueError(
                '`BindspaceSpec.key` must be non-empty or `None`!'
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
        if self.key == '':
            raise ValueError(
                '`BindspaceIdentity.key` must be non-empty '
                'or `None`!'
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
