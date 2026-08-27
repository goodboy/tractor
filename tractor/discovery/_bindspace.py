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
Serializable bindspace declarations and live capabilities.

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
BindspaceLifecycle: TypeAlias = Literal[
    'attach',  # borrow one existing platform resource
    'open',  # create, own and remove one platform resource
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


def _validate_bindspace_lifecycle(
    lifecycle: BindspaceLifecycle,
) -> None:
    '''
    Reject lifecycle policies without an implementation.

    '''
    if lifecycle not in get_args(BindspaceLifecycle):
        raise ValueError(
            f'Unsupported bindspace lifecycle: {lifecycle!r}'
        )


def _validate_bindspace_key(
    kind: BindspaceKind,
    key: str|None,
    field: str,
) -> None:
    '''
    Reject empty or path-like platform-resource names.

    `None` is valid. Spell it `CURRENT_NETNS` for
    `BindspaceSpec.key`; `BindspaceRef.key = None` records an
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
    lifecycle: BindspaceLifecycle = 'attach'

    def __post_init__(self) -> None:
        '''
        Reject an empty platform-resource key.

        '''
        _validate_bindspace_kind(self.kind)
        _validate_bindspace_lifecycle(self.lifecycle)
        _validate_bindspace_key(
            self.kind,
            self.key,
            'BindspaceSpec.key',
        )


class BindspaceRef(
    msgspec.Struct,
    frozen=True,
):
    '''
    Serializable, non-owning ref to one realized bindspace.

    `.key` is an optional mutable namespace locator. `.inode` is a
    host-local kernel fingerprint which remains stable while the
    resource exists or a live `Bindspace` pins it. This ref grants no
    authority and cannot reopen the resource by itself.

    '''
    kind: BindspaceKind
    key: str|None
    inode: int

    def __post_init__(self) -> None:
        '''
        Require a host-local resource inode and an optional locator.

        '''
        _validate_bindspace_kind(self.kind)
        _validate_bindspace_key(
            self.kind,
            self.key,
            'BindspaceRef.key',
        )
        if (
            type(self.inode) is not int
            or
            self.inode <= 0
        ):
            raise ValueError(
                '`BindspaceRef.inode` must be a positive `int`!'
            )


class Bindspace(
    ProcessLocal,
):
    '''
    Process-local capability for one live realized bindspace.

    `ProcessLocal` provides compact typed storage plus a default
    wire-encoding guard. `Bindspace` construction and explicit FD
    transfer belong to the supervisor's spawn/bootstrap path.

    '''
    spec: BindspaceSpec
    ref: BindspaceRef
    namespace_fd: int|None
    ownership: BindspaceOwnership

    def __post_init__(self) -> None:
        '''
        Validate and retain one scoped bindspace capability.

        '''
        spec: BindspaceSpec = self.spec
        ref: BindspaceRef = self.ref
        namespace_fd: int|None = self.namespace_fd
        ownership: BindspaceOwnership = self.ownership

        if spec.kind != ref.kind:
            raise ValueError(
                '`BindspaceSpec.kind` does not match '
                '`BindspaceRef.kind`!'
            )
        if (
            spec.key is not None
            and
            spec.key != ref.key
        ):
            raise ValueError(
                '`BindspaceSpec.key` does not match '
                '`BindspaceRef.key`!'
            )
        if ownership not in get_args(BindspaceOwnership):
            raise ValueError(
                f'Invalid bindspace ownership: {ownership!r}'
            )
        expected_ownership: BindspaceOwnership = (
            'borrowed'
            if spec.lifecycle == 'attach'
            else 'owned'
        )
        if ownership != expected_ownership:
            raise ValueError(
                f'`BindspaceSpec.lifecycle={spec.lifecycle!r}` '
                f'requires ownership={expected_ownership!r}!'
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
            if ref.inode != fd_inode:
                raise ValueError(
                    f'Namespace FD inode {fd_inode} does not match '
                    f'reference inode {ref.inode}!'
                )

    def __repr__(self) -> str:
        '''
        Render the capability ref without dereferencing its FD.

        '''
        return (
            f'{type(self).__name__}('
            f'ref={self.ref!r}, '
            f'ownership={self.ownership!r}, '
            f'namespace_fd={self.namespace_fd!r})'
        )


@acm
async def _pin_netns(
    spec: BindspaceSpec,
    ownership: BindspaceOwnership,
) -> AsyncIterator[Bindspace]:
    '''
    Pin one existing Linux network namespace with explicit ownership.

    '''
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
        ref: BindspaceRef = BindspaceRef(
            kind='netns',
            key=key,
            inode=inode,
        )
        bindspace: Bindspace = Bindspace(
            spec=spec,
            ref=ref,
            namespace_fd=namespace_fd,
            ownership=ownership,
        )
        yield bindspace
    finally:
        os.close(namespace_fd)


@acm
async def attach_netns(
    spec: BindspaceSpec,
) -> AsyncIterator[Bindspace]:
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
    if spec.lifecycle != 'attach':
        raise ValueError(
            '`attach_netns()` requires lifecycle=`attach`!'
        )
    async with _pin_netns(
        spec,
        ownership='borrowed',
    ) as bindspace:
        yield bindspace


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
) -> AsyncIterator[Bindspace]:
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
    if spec.lifecycle != 'open':
        raise ValueError(
            '`open_netns()` requires lifecycle=`open`!'
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

        async with _pin_netns(
            spec,
            ownership='owned',
        ) as bindspace:
            yield bindspace
    finally:
        if created:
            with trio.CancelScope(shield=True):
                await trio.to_thread.run_sync(
                    _remove_netns,
                    key,
                    abandon_on_cancel=False,
                )


@acm
async def open_bindspace(
    spec: BindspaceSpec,
) -> AsyncIterator[Bindspace]:
    '''
    Dispatch one declared bindspace lifecycle.

    Lifecycle is explicit serialized policy. It is never inferred
    from whether the eventual transport role is listen or dial.

    '''
    if spec.lifecycle == 'attach':
        async with attach_netns(spec) as bindspace:
            yield bindspace
    else:
        async with open_netns(spec) as bindspace:
            yield bindspace
