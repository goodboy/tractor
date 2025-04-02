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

import trio
import tractor
from tractor.linux import send_fds, recv_fds

import tractor.ipc._ringbuf as ringbuf
from tractor.ipc._ringbuf import RBToken


log = tractor.log.get_logger(__name__)
# log = tractor.log.get_console_log(level='info')


_ringd_actor_name = 'ringd'
_root_key = _ringd_actor_name + f'-{os.getpid()}'
_rings: dict[str, RBToken] = {}


async def _attach_to_ring(
    ring_name: str
) -> RBToken:
    actor = tractor.current_actor()

    fd_amount = 3
    sock_path = str(
        Path(tempfile.gettempdir())
        /
        f'{os.getpid()}-pass-ring-fds-{ring_name}-to-{actor.name}.sock'
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

    token = _rings[name]

    async with send_fds(token.fds, sock_path):
        log.info(f'connected to {sock_path} for fd passing')
        await ctx.started(token)

    log.info(f'fds {token.fds} sent')

    return token


@tractor.context
async def _open_ringbuf(
    ctx: tractor.Context,
    name: str,
    must_exist: bool = False,
    buf_size: int = 10 * 1024
):
    global _root_key, _rings

    teardown = trio.Event()
    async def _teardown_listener(task_status=trio.TASK_STATUS_IGNORED):
        async with ctx.open_stream() as stream:
            task_status.started()
            await stream.receive()
            teardown.set()

    log.info(f'maybe open ring {name}, must_exist = {must_exist}')

    token = _rings.get(name, None)

    async with trio.open_nursery() as n:
        if token:
            log.info(f'ring {name} exists')
            await ctx.started()
            await n.start(_teardown_listener)
            await teardown.wait()
            return

        if must_exist:
            raise FileNotFoundError(
                f'Tried to open_ringbuf but it doesn\'t exist: {name}'
            )

        with ringbuf.open_ringbuf(
            _root_key + name,
            buf_size=buf_size
        ) as token:
            _rings[name] = token
            log.info(f'ring {name} created')
            await ctx.started()
            await n.start(_teardown_listener)
            await teardown.wait()
            del _rings[name]

    log.info(f'ring {name} destroyed')


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
    must_exist: bool = False,
    buf_size: int = 10 * 1024
) -> RBToken:
    async with (
        wait_for_ringd() as ringd,
        ringd.open_context(
            _open_ringbuf,
            name=name,
            must_exist=must_exist,
            buf_size=buf_size
        ) as (rd_ctx, _),
        rd_ctx.open_stream() as stream,
    ):
        token = await _attach_to_ring(name)
        log.info(f'attached to {token}')
        yield token
        await stream.send(b'bye')

