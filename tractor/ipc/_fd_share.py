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
File-descriptor-sharing on `linux` by "wilhelm_of_bohemia".

'''
from __future__ import annotations
import os
import array
import socket
import tempfile
from pathlib import Path
from contextlib import ExitStack

import trio
import tractor
from tractor.ipc import RBToken


actor_name = 'ringd'


_rings: dict[str, dict] = {}


async def _attach_to_ring(
    ring_name: str
) -> tuple[int, int, int]:
    actor = tractor.current_actor()

    fd_amount = 3
    sock_path = (
        Path(tempfile.gettempdir())
        /
        f'{os.getpid()}-pass-ring-fds-{ring_name}-to-{actor.name}.sock'
    )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(sock_path)
    sock.listen(1)

    async with (
        tractor.find_actor(actor_name) as ringd,
        ringd.open_context(
            _pass_fds,
            name=ring_name,
            sock_path=sock_path
        ) as (ctx, _sent)
    ):
        # prepare array to receive FD
        fds = array.array("i", [0] * fd_amount)

        conn, _ = sock.accept()

        # receive FD
        msg, ancdata, flags, addr = conn.recvmsg(
            1024,
            socket.CMSG_LEN(fds.itemsize * fd_amount)
        )

        for (
            cmsg_level,
            cmsg_type,
            cmsg_data,
        ) in ancdata:
            if (
                cmsg_level == socket.SOL_SOCKET
                and
                cmsg_type == socket.SCM_RIGHTS
            ):
                fds.frombytes(cmsg_data[:fds.itemsize * fd_amount])
                break
            else:
                raise RuntimeError("Receiver: No FDs received")

        conn.close()
        sock.close()
        sock_path.unlink()

        return RBToken.from_msg(
            await ctx.wait_for_result()
        )


@tractor.context
async def _pass_fds(
    ctx: tractor.Context,
    name: str,
    sock_path: str
) -> RBToken:
    global _rings
    token = _rings[name]
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(sock_path)
    await ctx.started()
    fds = array.array('i', token.fds)
    client.sendmsg([b'FDs'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])
    client.close()
    return token


@tractor.context
async def _open_ringbuf(
    ctx: tractor.Context,
    name: str,
    buf_size: int
) -> RBToken:
    global _rings
    is_owner = False
    if name not in _rings:
        stack = ExitStack()
        token = stack.enter_context(
            tractor.open_ringbuf(
                name,
                buf_size=buf_size
            )
        )
        _rings[name] = {
            'token': token,
            'stack': stack,
        }
        is_owner = True

    ring = _rings[name]
    await ctx.started()

    try:
        await trio.sleep_forever()

    except tractor.ContextCancelled:
        ...

    finally:
        if is_owner:
            ring['stack'].close()


async def open_ringbuf(
    name: str,
    buf_size: int
) -> RBToken:
    async with (
        tractor.find_actor(actor_name) as ringd,
        ringd.open_context(
            _open_ringbuf,
            name=name,
            buf_size=buf_size
        ) as (rd_ctx, _)
    ):
        yield await _attach_to_ring(name)
        await rd_ctx.cancel()
