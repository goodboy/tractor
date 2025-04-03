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
Reimplementation of multiprocessing.reduction.sendfds & recvfds, using acms and trio.

cpython impl:
https://github.com/python/cpython/blob/275056a7fdcbe36aaac494b4183ae59943a338eb/Lib/multiprocessing/reduction.py#L138
'''
import array
from typing import AsyncContextManager
from contextlib import asynccontextmanager as acm

import trio
from trio import socket


class FDSharingError(Exception):
    ...


@acm
async def send_fds(fds: list[int], sock_path: str) -> AsyncContextManager[None]:
    '''
    Async trio reimplementation of `multiprocessing.reduction.sendfds`

    https://github.com/python/cpython/blob/275056a7fdcbe36aaac494b4183ae59943a338eb/Lib/multiprocessing/reduction.py#L142

    It's implemented using an async context manager in order to simplyfy usage
    with `tractor.context`s, we can open a context in a remote actor that uses
    this acm inside of it, and uses `ctx.started()` to signal the original
    caller actor to perform the `recv_fds` call.

    See `tractor.ipc._ringbuf._ringd._attach_to_ring` for an example.
    '''
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    await sock.bind(sock_path)
    sock.listen(1)

    yield  # socket is setup, ready for receiver connect

    # wait until receiver connects
    conn, _ = await sock.accept()

    # setup int array for fds
    fds = array.array('i', fds)

    # first byte of msg will be len of fds to send % 256, acting as a fd amount
    # verification on `recv_fds` we refer to it as `check_byte`
    msg = bytes([len(fds) % 256])

    # send msg with custom SCM_RIGHTS type
    await conn.sendmsg(
        [msg],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)]
    )

    # finally wait receiver ack
    if await conn.recv(1) != b'A':
        raise FDSharingError('did not receive acknowledgement of fd')

    conn.close()
    sock.close()


async def recv_fds(sock_path: str, amount: int) -> tuple:
    '''
    Async trio reimplementation of `multiprocessing.reduction.recvfds`

    https://github.com/python/cpython/blob/275056a7fdcbe36aaac494b4183ae59943a338eb/Lib/multiprocessing/reduction.py#L150

    It's equivalent to std just using `trio.open_unix_socket` for connecting and
    changes on error handling.

    See `tractor.ipc._ringbuf._ringd._attach_to_ring` for an example.
    '''
    stream = await trio.open_unix_socket(sock_path)
    sock = stream.socket

    # prepare int array for fds
    a = array.array('i')
    bytes_size = a.itemsize * amount

    # receive 1 byte + space necesary for SCM_RIGHTS msg for {amount} fds
    msg, ancdata, flags, addr = await sock.recvmsg(
        1, socket.CMSG_SPACE(bytes_size)
    )

    # maybe failed to receive msg?
    if not msg and not ancdata:
        raise FDSharingError(f'Expected to receive {amount} fds from {sock_path}, but got EOF')

    # send ack, std comment mentions this ack pattern was to get around an
    # old macosx bug, but they are not sure if its necesary any more, in
    # any case its not a bad pattern to keep
    await sock.send(b'A')  # Ack

    # expect to receive only one `ancdata` item
    if len(ancdata) != 1:
        raise FDSharingError(
            f'Expected to receive exactly one \"ancdata\" but got {len(ancdata)}: {ancdata}'
        )

    # unpack SCM_RIGHTS msg
    cmsg_level, cmsg_type, cmsg_data = ancdata[0]

    # check proper msg type
    if cmsg_level != socket.SOL_SOCKET:
        raise FDSharingError(
            f'Expected CMSG level to be SOL_SOCKET({socket.SOL_SOCKET}) but got {cmsg_level}'
        )

    if cmsg_type != socket.SCM_RIGHTS:
        raise FDSharingError(
            f'Expected CMSG type to be SCM_RIGHTS({socket.SCM_RIGHTS}) but got {cmsg_type}'
        )

    # check proper data alignment
    length = len(cmsg_data)
    if length % a.itemsize != 0:
        raise FDSharingError(
            f'CMSG data alignment error: len of {length} is not divisible by int size {a.itemsize}'
        )

    # attempt to cast as int array
    a.frombytes(cmsg_data)

    # validate length check byte
    valid_check_byte = amount % 256  # check byte acording to `recv_fds` caller
    recvd_check_byte = msg[0]  # actual received check byte
    payload_check_byte = len(a) % 256  # check byte acording to received fd int array

    if recvd_check_byte != payload_check_byte:
        raise FDSharingError(
            'Validation failed: received check byte '
            f'({recvd_check_byte}) does not match fd int array len % 256 ({payload_check_byte})'
        )

    if valid_check_byte != recvd_check_byte:
        raise FDSharingError(
            'Validation failed: received check byte '
            f'({recvd_check_byte}) does not match expected fd amount % 256 ({valid_check_byte})'
        )

    return tuple(a)
