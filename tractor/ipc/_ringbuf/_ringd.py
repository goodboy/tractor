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
Actor to broker ringbuf resources, creates and allocates
the resources, then automatically does fd passing.

call open_ringd in your root actor

then on actors that need a ringbuf resource use

open_ringbuf acm, will automatically contact ringd.
'''
import os
import tempfile
from pathlib import Path
from contextlib import (
    asynccontextmanager as acm
)
from dataclasses import dataclass

import trio
import tractor
from tractor.linux import send_fds, recv_fds

import tractor.ipc._ringbuf as ringbuf
from tractor.ipc._ringbuf import RBToken


log = tractor.log.get_logger(__name__)
# log = tractor.log.get_console_log(level='info')


class RingNotFound(Exception):
    ...


_ringd_actor_name = 'ringd'
_root_key = _ringd_actor_name + f'-{os.getpid()}'


@dataclass
class RingInfo:
    token: RBToken
    creator: str
    unlink: trio.Event()


_rings: dict[str, RingInfo] = {}


def _maybe_get_ring(name: str) -> RingInfo | None:
    if name in _rings:
        return _rings[name]

    return None


def _insert_ring(name: str, info: RingInfo):
    _rings[name] = info


def _destroy_ring(name: str):
    del _rings[name]


async def _attach_to_ring(
    ringd_pid: int,
    ring_name: str
) -> RBToken:
    actor = tractor.current_actor()

    fd_amount = 3
    sock_path = str(
        Path(tempfile.gettempdir())
        /
        f'ringd-{ringd_pid}-{ring_name}-to-{actor.name}.sock'
    )

    log.info(f'trying to attach to ring {ring_name}...')

    async with (
        tractor.find_actor(_ringd_actor_name) as ringd,
        ringd.open_context(
            _pass_fds,
            name=ring_name,
            sock_path=sock_path
        ) as (ctx, token),
    ):
        fds = await recv_fds(sock_path, fd_amount)
        log.info(
            f'received fds: {fds}'
        )

        token = RBToken.from_msg(token)

        write, wrap, eof = fds

        return RBToken(
            shm_name=token.shm_name,
            write_eventfd=write,
            wrap_eventfd=wrap,
            eof_eventfd=eof,
            buf_size=token.buf_size
        )


@tractor.context
async def _pass_fds(
    ctx: tractor.Context,
    name: str,
    sock_path: str
):
    global _rings
    info = _maybe_get_ring(name)

    if not info:
        raise RingNotFound(f'Ring \"{name}\" not found!')

    token = info.token

    async with send_fds(token.fds, sock_path):
        log.info(f'connected to {sock_path} for fd passing')
        await ctx.started(token)

    log.info(f'fds {token.fds} sent')

    return token


@tractor.context
async def _open_ringbuf(
    ctx: tractor.Context,
    caller: str,
    name: str,
    buf_size: int = 10 * 1024,
    must_exist: bool = False,
):
    global _root_key, _rings
    log.info(f'maybe open ring {name} from {caller}, must_exist = {must_exist}')

    info = _maybe_get_ring(name)

    if info:
        log.info(f'ring {name} exists, {caller} attached')

        await ctx.started(os.getpid())

        async with ctx.open_stream() as stream:
            await stream.receive()

        info.unlink.set()

        log.info(f'{caller} detached from ring {name}')

        return

    if must_exist:
        raise RingNotFound(
            f'Tried to open_ringbuf but it doesn\'t exist: {name}'
        )

    with ringbuf.open_ringbuf(
        _root_key + name,
        buf_size=buf_size
    ) as token:
        unlink_event = trio.Event()
        _insert_ring(
            name,
            RingInfo(
                token=token,
                creator=caller,
                unlink=unlink_event,
            )
        )
        log.info(f'ring {name} created by {caller}')
        await ctx.started(os.getpid())

        async with ctx.open_stream() as stream:
            await stream.receive()

        await unlink_event.wait()
        _destroy_ring(name)

    log.info(f'ring {name} destroyed by {caller}')


@acm
async def open_ringd(**kwargs) -> tractor.Portal:
    async with tractor.open_nursery(**kwargs) as an:
        portal = await an.start_actor(
            _ringd_actor_name,
            enable_modules=[__name__]
        )
        yield portal
        await an.cancel()


@acm
async def wait_for_ringd() -> tractor.Portal:
    async with tractor.wait_for_actor(
        _ringd_actor_name
    ) as portal:
        yield portal


@acm
async def open_ringbuf(

    name: str,
    buf_size: int = 10 * 1024,

    must_exist: bool = False,

) -> RBToken:
    actor = tractor.current_actor()
    async with (
        wait_for_ringd() as ringd,

        ringd.open_context(
            _open_ringbuf,
            caller=actor.name,
            name=name,
            buf_size=buf_size,
            must_exist=must_exist
        ) as (rd_ctx, ringd_pid),

        rd_ctx.open_stream() as _stream,
    ):
        token = await _attach_to_ring(ringd_pid, name)
        log.info(f'attached to {token}')
        yield token

