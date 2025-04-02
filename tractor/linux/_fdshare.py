'''
Re-Impl of multiprocessing.reduction.sendfds & recvfds,
using acms and trio
'''
import array
from contextlib import asynccontextmanager as acm

import trio
from trio import socket


@acm
async def send_fds(fds: list[int], sock_path: str):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    await sock.bind(sock_path)
    sock.listen(1)
    fds = array.array('i', fds)
    # first byte of msg will be len of fds to send % 256
    msg = bytes([len(fds) % 256])
    yield
    conn, _ = await sock.accept()
    await conn.sendmsg(
        [msg],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)]
    )
    # wait ack
    if await conn.recv(1) != b'A':
        raise RuntimeError('did not receive acknowledgement of fd')

    conn.close()
    sock.close()


@acm
async def recv_fds(sock_path: str, amount: int) -> tuple:
    stream = await trio.open_unix_socket(sock_path)
    sock = stream.socket
    a = array.array('i')
    bytes_size = a.itemsize * amount
    msg, ancdata, flags, addr = await sock.recvmsg(
        1, socket.CMSG_SPACE(bytes_size)
    )
    if not msg and not ancdata:
        raise EOFError
    try:
        await sock.send(b'A')  # Ack

        if len(ancdata) != 1:
            raise RuntimeError(
                f'received {len(ancdata)} items of ancdata'
            )

        cmsg_level, cmsg_type, cmsg_data = ancdata[0]
        # check proper msg type
        if (
            cmsg_level == socket.SOL_SOCKET
            and
            cmsg_type == socket.SCM_RIGHTS
        ):
            # check proper data alignment
            if len(cmsg_data) % a.itemsize != 0:
                raise ValueError

            # attempt to cast as int array
            a.frombytes(cmsg_data)

            # check first byte of message is amount % 256
            if len(a) % 256 != msg[0]:
                raise AssertionError(
                    'Len is {0:n} but msg[0] is {1!r}'.format(
                        len(a), msg[0]
                    )
                )

            yield tuple(a)
            return

    except (ValueError, IndexError):
        pass

    raise RuntimeError('Invalid data received')
