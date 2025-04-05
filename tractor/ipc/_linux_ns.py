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
Utils for Linux namespaces.

See `man namespaces` for the high level deats!

'''
import subprocess
import os
from pathlib import Path


def get_ns_inode(
    ns_type: str = 'net',
    pid: int = os.getpid(),
) -> int:
    '''
    Return the `inode: int` for the namespace type `ns_type: str` for
    the current process.

    By default we use the  "network namespace" since it is generallly
    used to isolate all the networ-transport domains for all IPC
    inside `tractor.ipc`.

    See for linux:
    - `man namespaces`,
    - `man network_namespaces`,
     > In addition, network namespaces isolate the UNIX domain
       abstract socket name‐ space (see unix(7))

    - `man unix`,

    If pid: int` is passed then do the lookup to
    `Path(f'/proc/{pid}/ns/{ns_type}') instead of for the current
    pid.

    '''
    ns_dir = Path(f'/proc/{pid}/ns/')
    ns_path: Path = ns_dir / ns_type
    entry: str = os.readlink(f'{ns_path}')
    _ns_type: str
    boxed_inode: str
    _ns_type, _, boxed_inode = entry.partition(':')
    assert ns_type == _ns_type

    inode_str: str = boxed_inode[1:-1]
    return int(inode_str)


def get_netns(pid: int = os.getpid()) -> tuple[str|None, int]:
    '''
    Return the network namespace key-inode pair for the given process
    id.

    '''
    out: str = subprocess.check_ouput([
        'ip',
        'netns',
        'identify',
        pid,
    ])
    netns_key: str|None = None
    if out.decode().strip():
        netns_key: str = out

    # signifies the "default" (really just not in use)
    # net namespace.
    return (
        netns_key or 'default',
        get_ns_inode(
            ns_type='net',
            pid=pid,
        ),
    )
