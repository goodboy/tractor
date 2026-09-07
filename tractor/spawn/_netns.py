# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
Linux network-namespace actor-bootstrap primitives.

'''
from __future__ import annotations

from collections.abc import (
    Callable,
    Iterator,
)
from contextlib import contextmanager as cm
import errno
from pathlib import Path
import os
import sys
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from ..net._bindspace import Bindspace


# `setns(2)` mutates only the calling thread's namespace:
# https://man7.org/linux/man-pages/man2/setns.2.html
# `/proc/thread-self` addresses the caller's current task:
# https://man7.org/linux/man-pages/man5/proc_pid_task.5.html
# Do not use `/proc/self` because it resolves through the process
# leader.
_SELF_NETNS: Path = Path('/proc/thread-self/ns/net')


def enter_netns(
    namespace_fd: int,
    expected_device: int,
    expected_inode: int,
) -> int:
    '''
    Enter and verify one inherited Linux network namespace.

    The caller owns and closes `namespace_fd`.

    '''
    if sys.platform != 'linux':
        raise RuntimeError(
            'Network namespace entry is Linux-only!'
        )
    if (
        type(namespace_fd) is not int
        or
        namespace_fd < 0
    ):
        raise ValueError(
            '`namespace_fd` must be a non-negative `int`!'
        )
    if (
        type(expected_inode) is not int
        or
        expected_inode <= 0
    ):
        raise ValueError(
            '`expected_inode` must be a positive `int`!'
        )

    if type(expected_device) is not int or expected_device < 0:
        raise ValueError('`expected_device` must be a non-negative `int`!')

    target = os.fstat(namespace_fd)
    expected = (expected_device, expected_inode)
    if (target.st_dev, target.st_ino) != expected:
        raise ValueError(
            f'Inherited namespace FD identity '
            f'{(target.st_dev, target.st_ino)} does not '
            f'match expected identity {expected}!'
        )

    current = _SELF_NETNS.stat()
    if (current.st_dev, current.st_ino) == expected:
        return expected_inode

    setns: Callable[[int, int], None]|None = getattr(
        os,
        'setns',
        None,
    )
    clone_newnet: int|None = getattr(
        os,
        'CLONE_NEWNET',
        None,
    )
    if (
        setns is None
        or
        clone_newnet is None
    ):
        raise RuntimeError(
            'Python has no Linux network namespace entry support!'
        )

    try:
        setns(namespace_fd, clone_newnet)
    except OSError as exc:
        raise RuntimeError(
            f'Could not enter network namespace inode '
            f'{expected_inode}!'
        ) from exc

    entered = _SELF_NETNS.stat()
    entered_inode: int = entered.st_ino
    if (entered.st_dev, entered_inode) != expected:
        raise RuntimeError(
            f'Entered network namespace identity '
            f'{(entered.st_dev, entered_inode)} does not '
            f'match expected identity {expected}!'
        )

    return entered_inode


@cm
def close_fd(
    owned_fd: int,
    fd_name: str,
) -> Iterator[None]:
    '''
    Close one owned netns FD without masking a prior error.

    '''
    operation: str = (
        f'close owned {fd_name} netns FD {owned_fd}'
    )
    try:
        yield
    except BaseException as primary_error:
        try:
            os.close(owned_fd)
        except BaseException as close_error:
            primary_error.add_note(
                f'Also failed to {operation}: {close_error!r}'
            )
        raise primary_error
    else:
        try:
            os.close(owned_fd)
        except BaseException as close_error:
            close_error.add_note(
                f'Failed to {operation} during root netns cleanup.'
            )
            raise close_error


@cm
def dup_fd(
    source_fd: int,
) -> Iterator[int]:
    '''
    Duplicate and own the target netns FD for this context.

    '''
    try:
        owned_fd: int = os.dup(source_fd)
    except OSError as dup_error:
        if dup_error.errno != errno.EBADF:
            raise dup_error
        raise ValueError(
            '`bindspace.namespace_fd` does not reference '
            'a live FD!'
        ) from dup_error

    with close_fd(owned_fd, 'target'):
        yield owned_fd


@cm
def _enter_netns_temporarily(
    bindspace: Bindspace|None,
) -> Iterator[int|None]:
    '''
    Enter a root bindspace and restore the caller thread's netns.

    Internal synchronous primitive only. Never surround a Trio
    checkpoint with this scope: entry changes only the calling thread.
    The former async root adapter has been removed.

    Only descriptors opened or duplicated by this context are used
    for validation, entry and restoration. Since `setns()` is
    thread-local, this synchronous context performs no checkpoints
    around either transition.

    '''
    if bindspace is None:
        yield None
        return

    if sys.platform != 'linux':
        raise RuntimeError(
            'Network namespace entry is Linux-only!'
        )

    namespace_fd: int = bindspace.namespace_fd
    if (
        type(namespace_fd) is not int
        or
        namespace_fd < 0
    ):
        raise ValueError(
            '`bindspace.namespace_fd` must be a live '
            'non-negative FD!'
        )

    # Borrow `Bindspace.namespace_fd`; duplicate it so this context
    # owns target cleanup and cannot close the caller's capability.
    # Nested FD scopes aggregate later close failures as notes on the
    # first body, restoration or cleanup error.
    with dup_fd(namespace_fd) as tgt_fd:
        tgt_stat: os.stat_result = os.fstat(tgt_fd)
        tgt_inode: int = bindspace.ref.inode
        if tgt_stat.st_ino != tgt_inode:
            raise ValueError(
                f'Target namespace FD inode '
                f'{tgt_stat.st_ino} does '
                f'not match bindspace inode {tgt_inode}!'
            )

        # Capture the calling thread's current netns before any
        # transition. It need not be the process's initial netns. This
        # context owns the snapshot FD even when no transition is
        # needed, so keep it live through restoration and always close
        # it afterward.
        orig_fd = os.open(
            _SELF_NETNS,
            os.O_RDONLY | os.O_CLOEXEC,
        )
        with close_fd(orig_fd, 'original'):
            orig_stat: os.stat_result = os.fstat(orig_fd)
            orig_inode: int = orig_stat.st_ino
            restore_needed: bool = (
                tgt_stat.st_dev != orig_stat.st_dev
                or
                tgt_inode != orig_inode
            )
            try:
                if restore_needed:
                    enter_netns(
                        tgt_fd,
                        tgt_stat.st_dev,
                        tgt_inode,
                    )

                yield tgt_inode

            except BaseException as primary_error:
                if restore_needed:
                    try:
                        enter_netns(
                            orig_fd,
                            orig_stat.st_dev,
                            orig_inode,
                        )
                    except BaseException as restore_error:
                        primary_error.add_note(
                            'Also failed to restore the original '
                            'network namespace: '
                            f'{restore_error!r}'
                        )
                raise primary_error

            if restore_needed:
                try:
                    enter_netns(
                        orig_fd,
                        orig_stat.st_dev,
                        orig_inode,
                    )
                except BaseException as restore_error:
                    restore_error.add_note(
                        'Failed to restore the original network '
                        'namespace during root netns cleanup.'
                    )
                    raise restore_error


@cm
def pin_current_netns() -> Iterator[Bindspace]:
    '''
    Pin the calling task's netns synchronously, without entering it.

    '''
    from ..net._bindspace import Bindspace, BindspaceRef, BindspaceSpec

    namespace_fd = os.open(_SELF_NETNS, os.O_RDONLY | os.O_CLOEXEC)
    with close_fd(namespace_fd, 'calling task'):
        yield Bindspace(
            spec=BindspaceSpec(kind='netns'),
            ref=BindspaceRef(
                kind='netns',
                key=None,
                inode=os.fstat(namespace_fd).st_ino,
            ),
            namespace_fd=namespace_fd,
            ownership='borrowed',
        )
