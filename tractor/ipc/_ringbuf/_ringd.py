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
from typing import AsyncContextManager
from pathlib import Path
from contextlib import (
    asynccontextmanager as acm
)
from dataclasses import dataclass

import trio
import tractor
from tractor.linux import (
    send_fds,
    recv_fds,
)

import tractor.ipc._ringbuf as ringbuf
from tractor.ipc._ringbuf import RBToken


log = tractor.log.get_logger(__name__)


'''
Daemon implementation

'''


_ringd_actor_name: str = 'ringd'


_root_name: str = f'{_ringd_actor_name}-{os.getpid()}'


def _make_ring_name(name: str) -> str:
    '''
    User provided ring names will be prefixed by the ringd actor name and pid.
    '''
    return f'{_root_name}.{name}'


@dataclass
class RingInfo:
    token: RBToken
    creator: str


_rings: dict[str, RingInfo] = {}
_ring_lock = trio.StrictFIFOLock()


def _maybe_get_ring(name: str) -> RingInfo | None:
    '''
    Maybe return RingInfo for a given name str

    '''
    # if full name was passed, strip root name
    if _root_name in name:
        name = name.replace(f'{_root_name}.', '')

    return _rings.get(name, None)


def _get_ring(name: str) -> RingInfo:
    '''
    Return a RingInfo for a given name or raise
    '''
    info = _maybe_get_ring(name)

    if not info:
        raise RuntimeError(f'Ring \"{name}\" not found!')

    return info


def _insert_ring(name: str, info: RingInfo):
    '''
    Add a new ring
    '''
    if name in _rings:
        raise RuntimeError(f'A ring with name {name} already exists!')

    _rings[name] = info


def _destroy_ring(name: str):
    '''
    Delete information about a ring
    '''
    if name not in _rings:
        raise RuntimeError(f'Tried to delete non existant {name} ring!')

    del _rings[name]


@tractor.context
async def _pass_fds(
    ctx: tractor.Context,
    name: str,
    sock_path: str
):
    '''
    Ringd endpoint to request passing fds of a ring.

    Supports passing fullname or not (ringd actor name and pid before ring
    name).

    See `_attach_to_ring` function for usage.
    '''
    async with _ring_lock:
        # get ring fds or raise error
        token = _get_ring(name).token

        # start fd passing context using socket on `sock_path`
        async with send_fds(token.fds, sock_path):
            log.info(f'connected to {sock_path} for fd passing')
            # use started to signal socket is ready and send token in order for
            # client to get extra info like buf_size
            await ctx.started(token)
            # send_fds will block until receive side acks

    log.info(f'ring {name} fds: {token.fds}, sent')


@tractor.context
async def _open_ringbuf(
    ctx: tractor.Context,
    caller: str,
    name: str,
    buf_size: int = 10 * 1024
):
    '''
    Ringd endpoint to create and allocate resources for a new ring.

    '''
    await _ring_lock.acquire()
    maybe_info = _maybe_get_ring(name)

    if maybe_info:
        raise RuntimeError(
            f'Tried to create ringbuf but it already exists: {name}'
        )

    fullname = _make_ring_name(name)

    with ringbuf.open_ringbuf(
        fullname,
        buf_size=buf_size
    ) as token:

        _insert_ring(
            name,
            RingInfo(
                token=token,
                creator=caller,
            )
        )

        _ring_lock.release()

        # yield full ring name to rebuild token after fd passing
        await ctx.started(fullname)

        # await ctx cancel to remove ring from tracking and cleanup
        try:
            log.info(f'ring {name} created by {caller}')
            await trio.sleep_forever()

        finally:
            _destroy_ring(name)

            log.info(f'ring {name} destroyed by {caller}')


@tractor.context
async def _attach_ringbuf(
    ctx: tractor.Context,
    caller: str,
    name: str
) -> str:
    '''
    Ringd endpoint to "attach" to an existing ring, this just ensures ring
    actually exists and returns its full name.
    '''
    async with _ring_lock:
        info = _maybe_get_ring(name)

        if not info:
            raise RuntimeError(
                f'{caller} tried to open_ringbuf but it doesn\'t exist: {name}'
            )

        await ctx.started()

        # return full ring name to rebuild token after fd passing
        return info.token.shm_name


@tractor.context
async def _maybe_open_ringbuf(
    ctx: tractor.Context,
    caller: str,
    name: str,
    buf_size: int = 10 * 1024,
):
    '''
    If ring already exists attach, if not create it.
    '''
    maybe_info = _maybe_get_ring(name)

    if maybe_info:
        return await _attach_ringbuf(ctx, caller, name)

    return await _open_ringbuf(ctx, caller, name, buf_size=buf_size)


'''
Ringd client side helpers

'''


@acm
async def open_ringd(**kwargs) -> tractor.Portal:
    '''
    Spawn new ringd actor.

    '''
    async with tractor.open_nursery(**kwargs) as an:
        portal = await an.start_actor(
            _ringd_actor_name,
            enable_modules=[__name__]
        )
        yield portal
        await an.cancel()


@acm
async def wait_for_ringd() -> tractor.Portal:
    '''
    Wait for ringd actor to be up.

    '''
    async with tractor.wait_for_actor(
        _ringd_actor_name
    ) as portal:
        yield portal


async def _request_ring_fds(
    fullname: str
) -> RBToken:
    '''
    Private helper to fetch ring fds from ringd actor.
    '''
    actor = tractor.current_actor()

    fd_amount = 3
    sock_path = str(
        Path(tempfile.gettempdir())
        /
        f'{fullname}-to-{actor.name}.sock'
    )

    log.info(f'trying to attach to {fullname}...')

    async with (
        tractor.find_actor(_ringd_actor_name) as ringd,

        ringd.open_context(
            _pass_fds,
            name=fullname,
            sock_path=sock_path
        ) as (ctx, token),
    ):
        fds = await recv_fds(sock_path, fd_amount)
        write, wrap, eof = fds
        log.info(
            f'received fds, write: {write}, wrap: {wrap}, eof: {eof}'
        )

        token = RBToken.from_msg(token)

        return RBToken(
            shm_name=fullname,
            write_eventfd=write,
            wrap_eventfd=wrap,
            eof_eventfd=eof,
            buf_size=token.buf_size
        )



@acm
async def open_ringbuf(
    name: str,
    buf_size: int = 10 * 1024,
) -> AsyncContextManager[RBToken]:
    '''
    Create a new ring and retrieve its fds.

    '''
    actor = tractor.current_actor()
    async with (
        wait_for_ringd() as ringd,

        ringd.open_context(
            _open_ringbuf,
            caller=actor.name,
            name=name,
            buf_size=buf_size,
        ) as (ctx, fullname),
    ):
        token = await _request_ring_fds(fullname)
        log.info(f'{actor.name} opened {token}')
        try:
            yield token

        finally:
            with trio.CancelScope(shield=True):
                await ctx.cancel()


@acm
async def attach_ringbuf(
    name: str,
) -> AsyncContextManager[RBToken]:
    '''
    Attach to an existing ring and retreive its fds.

    '''
    actor = tractor.current_actor()
    async with (
        wait_for_ringd() as ringd,

        ringd.open_context(
            _attach_ringbuf,
            caller=actor.name,
            name=name,
        ) as (ctx, _),
    ):
        fullname = await ctx.wait_for_result()
        token = await _request_ring_fds(fullname)
        log.info(f'{actor.name} attached {token}')
        yield token


@acm
async def maybe_open_ringbuf(
    name: str,
    buf_size: int = 10 * 1024,
) -> AsyncContextManager[RBToken]:
    '''
    Attach or create a ring and retreive its fds.

    '''
    actor = tractor.current_actor()
    async with (
        wait_for_ringd() as ringd,

        ringd.open_context(
            _maybe_open_ringbuf,
            caller=actor.name,
            name=name,
            buf_size=buf_size,
        ) as (ctx, fullname),
    ):
        token = await _request_ring_fds(fullname)
        log.info(f'{actor.name} opened {token}')
        try:
            yield token

        finally:
            with trio.CancelScope(shield=True):
                await ctx.cancel()
