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

from collections.abc import Callable
from pathlib import Path
import os
import sys


_SELF_NETNS: Path = Path('/proc/self/ns/net')


def enter_netns(
    namespace_fd: int,
    expected_inode: int,
) -> int:
    '''
    Enter and verify one inherited Linux network namespace.

    The future spawn-bootstrap caller owns and closes `namespace_fd`.

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

    inherited_inode: int = os.fstat(namespace_fd).st_ino
    if inherited_inode != expected_inode:
        raise ValueError(
            f'Inherited namespace FD inode {inherited_inode} does not '
            f'match expected inode {expected_inode}!'
        )

    try:
        setns(namespace_fd, clone_newnet)
    except OSError as exc:
        raise RuntimeError(
            f'Could not enter network namespace inode '
            f'{expected_inode}!'
        ) from exc

    entered_inode: int = _SELF_NETNS.stat().st_ino
    if entered_inode != expected_inode:
        raise RuntimeError(
            f'Entered network namespace inode {entered_inode} does not '
            f'match expected inode {expected_inode}!'
        )

    return entered_inode
