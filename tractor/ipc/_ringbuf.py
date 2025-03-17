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
IPC Reliable RingBuffer implementation

'''
from __future__ import annotations
import struct
from collections.abc import (
    AsyncGenerator,
    AsyncIterator
)
from contextlib import (
    contextmanager as cm,
    asynccontextmanager as acm
)
from typing import (
    Any
)
from multiprocessing.shared_memory import SharedMemory

import trio
from tricycle import BufferedReceiveStream
from msgspec import (
    Struct,
    to_builtins
)

from ._linux import (
    open_eventfd,
    close_eventfd,
    EFDReadCancelled,
    EventFD
)
from ._mp_bs import disable_mantracker
from tractor.log import get_logger
from tractor._exceptions import (
    TransportClosed,
    InternalError
)
from tractor.ipc import MsgTransport


log = get_logger(__name__)


disable_mantracker()

_DEFAULT_RB_SIZE = 10 * 1024


class RBToken(Struct, frozen=True):
    '''
    RingBuffer token contains necesary info to open the three
    eventfds and the shared memory

    '''
    shm_name: str

    write_eventfd: int  # used to signal writer ptr advance
    wrap_eventfd: int  # used to signal reader ready after wrap around
    eof_eventfd: int  # used to signal writer closed

    buf_size: int

    def as_msg(self):
        return to_builtins(self)

    @classmethod
    def from_msg(cls, msg: dict) -> RBToken:
        if isinstance(msg, RBToken):
            return msg

        return RBToken(**msg)

    @property
    def fds(self) -> tuple[int, int, int]:
        '''
        Useful for `pass_fds` params

        '''
        return (
            self.write_eventfd,
            self.wrap_eventfd,
            self.eof_eventfd
        )


@cm
def open_ringbuf(
    shm_name: str,
    buf_size: int = _DEFAULT_RB_SIZE,
) -> RBToken:
    '''
    Handle resources for a ringbuf (shm, eventfd), yield `RBToken` to
    be used with `attach_to_ringbuf_sender` and `attach_to_ringbuf_receiver`

    '''
    shm = SharedMemory(
        name=shm_name,
        size=buf_size,
        create=True
    )
    try:
        token = RBToken(
            shm_name=shm_name,
            write_eventfd=open_eventfd(),
            wrap_eventfd=open_eventfd(),
            eof_eventfd=open_eventfd(),
            buf_size=buf_size
        )
        yield token
        try:
            close_eventfd(token.write_eventfd)

        except OSError:
            ...

        try:
            close_eventfd(token.wrap_eventfd)

        except OSError:
            ...

        try:
            close_eventfd(token.eof_eventfd)

        except OSError:
            ...

    finally:
        shm.unlink()


Buffer = bytes | bytearray | memoryview

'''
IPC Reliable Ring Buffer

`eventfd(2)` is used for wrap around sync, to signal writes to
the reader and end of stream.

'''


class RingBuffSender(trio.abc.SendStream):
    '''
    Ring Buffer sender side implementation

    Do not use directly! manage with `attach_to_ringbuf_sender`
    after having opened a ringbuf context with `open_ringbuf`.

    '''
    def __init__(
        self,
        token: RBToken,
        cleanup: bool = False
    ):
        self._token = RBToken.from_msg(token)
        self._shm: SharedMemory | None = None
        self._write_event = EventFD(self._token.write_eventfd, 'w')
        self._wrap_event = EventFD(self._token.wrap_eventfd, 'r')
        self._eof_event = EventFD(self._token.eof_eventfd, 'w')
        self._ptr = 0

        self._cleanup = cleanup
        self._send_lock = trio.StrictFIFOLock()

    @property
    def name(self) -> str:
        if not self._shm:
            raise ValueError('shared memory not initialized yet!')
        return self._shm.name

    @property
    def size(self) -> int:
        return self._token.buf_size

    @property
    def ptr(self) -> int:
        return self._ptr

    @property
    def write_fd(self) -> int:
        return self._write_event.fd

    @property
    def wrap_fd(self) -> int:
        return self._wrap_event.fd

    async def send_all(self, data: Buffer):
        async with self._send_lock:
            # while data is larger than the remaining buf
            target_ptr = self.ptr + len(data)
            while target_ptr > self.size:
                # write all bytes that fit
                remaining = self.size - self.ptr
                self._shm.buf[self.ptr:] = data[:remaining]
                # signal write and wait for reader wrap around
                self._write_event.write(remaining)
                await self._wrap_event.read()

                # wrap around and trim already written bytes
                self._ptr = 0
                data = data[remaining:]
                target_ptr = self._ptr + len(data)

            # remaining data fits on buffer
            self._shm.buf[self.ptr:target_ptr] = data
            self._write_event.write(len(data))
            self._ptr = target_ptr

    async def wait_send_all_might_not_block(self):
        raise NotImplementedError

    def open(self):
        self._shm = SharedMemory(
            name=self._token.shm_name,
            size=self._token.buf_size,
            create=False
        )
        self._write_event.open()
        self._wrap_event.open()
        self._eof_event.open()

    def close(self):
        self._eof_event.write(
            self._ptr if self._ptr > 0 else self.size
        )
        if self._cleanup:
            self._write_event.close()
            self._wrap_event.close()
            self._eof_event.close()
            self._shm.close()

    async def aclose(self):
        self.close()

    async def __aenter__(self):
        self.open()
        return self


class RingBuffReceiver(trio.abc.ReceiveStream):
    '''
    Ring Buffer receiver side implementation

    Do not use directly! manage with `attach_to_ringbuf_receiver`
    after having opened a ringbuf context with `open_ringbuf`.

    '''
    def __init__(
        self,
        token: RBToken,
        cleanup: bool = True,
    ):
        self._token = RBToken.from_msg(token)
        self._shm: SharedMemory | None = None
        self._write_event = EventFD(self._token.write_eventfd, 'w')
        self._wrap_event = EventFD(self._token.wrap_eventfd, 'r')
        self._eof_event = EventFD(self._token.eof_eventfd, 'r')
        self._ptr: int = 0
        self._write_ptr: int = 0
        self._end_ptr: int = -1

        self._cleanup: bool = cleanup

    @property
    def name(self) -> str:
        if not self._shm:
            raise ValueError('shared memory not initialized yet!')
        return self._shm.name

    @property
    def size(self) -> int:
        return self._token.buf_size

    @property
    def ptr(self) -> int:
        return self._ptr

    @property
    def write_fd(self) -> int:
        return self._write_event.fd

    @property
    def wrap_fd(self) -> int:
        return self._wrap_event.fd

    async def _eof_monitor_task(self):
        '''
        Long running EOF event monitor, automatically run in bg by
        `attach_to_ringbuf_receiver` context manager, if EOF event
        is set its value will be the end pointer (highest valid
        index to be read from buf, after setting the `self._end_ptr`
        we close the write event which should cancel any blocked
        `self._write_event.read()`s on it.

        '''
        try:
            self._end_ptr = await self._eof_event.read()
            self._write_event.close()

        except EFDReadCancelled:
            ...

    async def receive_some(self, max_bytes: int | None = None) -> bytes:
        '''
        Receive up to `max_bytes`, if no `max_bytes` is provided
        a reasonable default is used.

        '''
        if max_bytes is None:
            max_bytes: int = _DEFAULT_RB_SIZE

        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")

        # delta is remaining bytes we havent read
        delta = self._write_ptr - self._ptr
        if delta == 0:
            # we have read all we can, see if new data is available
            if self._end_ptr < 0:
                # if we havent been signaled about EOF yet
                try:
                    delta = await self._write_event.read()
                    self._write_ptr += delta

                except EFDReadCancelled:
                    # while waiting for new data `self._write_event` was closed
                    # this means writer signaled EOF
                    if self._end_ptr > 0:
                        # final self._write_ptr modification and recalculate delta
                        self._write_ptr = self._end_ptr
                        delta = self._end_ptr - self._ptr

                    else:
                        # shouldnt happen cause self._eof_monitor_task always sets
                        # self._end_ptr before closing self._write_event
                        raise InternalError(
                            'self._write_event.read cancelled but self._end_ptr is not set'
                        )

            else:
                # no more bytes to read and self._end_ptr set, EOF reached
                return b''

        # dont overflow caller
        delta = min(delta, max_bytes)

        target_ptr = self._ptr + delta

        # fetch next segment and advance ptr
        segment = bytes(self._shm.buf[self._ptr:target_ptr])
        self._ptr = target_ptr

        if self._ptr == self.size:
            # reached the end, signal wrap around
            self._ptr = 0
            self._write_ptr = 0
            self._wrap_event.write(1)

        return segment

    def open(self):
        self._shm = SharedMemory(
            name=self._token.shm_name,
            size=self._token.buf_size,
            create=False
        )
        self._write_event.open()
        self._wrap_event.open()
        self._eof_event.open()

    def close(self):
        if self._cleanup:
            self._write_event.close()
            self._wrap_event.close()
            self._eof_event.close()
            self._shm.close()

    async def aclose(self):
        self.close()

    async def __aenter__(self):
        self.open()
        return self


@acm
async def attach_to_ringbuf_receiver(
    token: RBToken,
    cleanup: bool = True
):
    '''
    Instantiate a RingBuffReceiver from a previously opened
    RBToken.

    Launches `receiver._eof_monitor_task` in a `trio.Nursery`.
    '''
    async with (
        trio.open_nursery() as n,
        RingBuffReceiver(
            token,
            cleanup=cleanup
        ) as receiver
    ):
        n.start_soon(receiver._eof_monitor_task)
        yield receiver

@acm
async def attach_to_ringbuf_sender(
    token: RBToken,
    cleanup: bool = True
):
    '''
    Instantiate a RingBuffSender from a previously opened
    RBToken.

    '''
    async with RingBuffSender(
        token,
        cleanup=cleanup
    ) as sender:
        yield sender


@cm
def open_ringbuf_pair(
    name: str,
    buf_size: int = _DEFAULT_RB_SIZE
):
    '''
    Handle resources for a ringbuf pair to be used for
    bidirectional messaging.

    '''
    with (
        open_ringbuf(
            name + '.pair0',
            buf_size=buf_size
        ) as token_0,

        open_ringbuf(
            name + '.pair1',
            buf_size=buf_size
        ) as token_1
    ):
        yield token_0, token_1


@acm
async def attach_to_ringbuf_pair(
    token_in: RBToken,
    token_out: RBToken,
    cleanup_in: bool = True,
    cleanup_out: bool = True
):
    '''
    Instantiate a trio.StapledStream from a previously opened
    ringbuf pair.

    '''
    async with (
        attach_to_ringbuf_receiver(
            token_in,
            cleanup=cleanup_in
        ) as receiver,
        attach_to_ringbuf_sender(
            token_out,
            cleanup=cleanup_out
        ) as sender,
    ):
        yield trio.StapledStream(sender, receiver)


class MsgpackRBStream(MsgTransport):

    def __init__(
        self,
        stream: trio.StapledStream
    ):
        self.stream = stream

        # create read loop intance
        self._aiter_pkts = self._iter_packets()
        self._send_lock = trio.StrictFIFOLock()

        self.drained: list[dict] = []

        self.recv_stream = BufferedReceiveStream(
            transport_stream=stream
        )

    async def _iter_packets(self) -> AsyncGenerator[dict, None]:
        '''
        Yield `bytes`-blob decoded packets from the underlying TCP
        stream using the current task's `MsgCodec`.

        This is a streaming routine implemented as an async generator
        func (which was the original design, but could be changed?)
        and is allocated by a `.__call__()` inside `.__init__()` where
        it is assigned to the `._aiter_pkts` attr.

        '''

        while True:
            try:
                header: bytes = await self.recv_stream.receive_exactly(4)
            except (
                ValueError,
                ConnectionResetError,

                # not sure entirely why we need this but without it we
                # seem to be getting racy failures here on
                # arbiter/registry name subs..
                trio.BrokenResourceError,

            ) as trans_err:

                loglevel = 'transport'
                match trans_err:
                    # case (
                    #     ConnectionResetError()
                    # ):
                    #     loglevel = 'transport'

                    # peer actor (graceful??) TCP EOF but `tricycle`
                    # seems to raise a 0-bytes-read?
                    case ValueError() if (
                        'unclean EOF' in trans_err.args[0]
                    ):
                        pass

                    # peer actor (task) prolly shutdown quickly due
                    # to cancellation
                    case trio.BrokenResourceError() if (
                        'Connection reset by peer' in trans_err.args[0]
                    ):
                        pass

                    # unless the disconnect condition falls under "a
                    # normal operation breakage" we usualy console warn
                    # about it.
                    case _:
                        loglevel: str = 'warning'


                raise TransportClosed(
                    message=(
                        f'IPC transport already closed by peer\n'
                        f'x)> {type(trans_err)}\n'
                        f' |_{self}\n'
                    ),
                    loglevel=loglevel,
                ) from trans_err

            # XXX definitely can happen if transport is closed
            # manually by another `trio.lowlevel.Task` in the
            # same actor; we use this in some simulated fault
            # testing for ex, but generally should never happen
            # under normal operation!
            #
            # NOTE: as such we always re-raise this error from the
            #       RPC msg loop!
            except trio.ClosedResourceError as closure_err:
                raise TransportClosed(
                    message=(
                        f'IPC transport already manually closed locally?\n'
                        f'x)> {type(closure_err)} \n'
                        f' |_{self}\n'
                    ),
                    loglevel='error',
                    raise_on_report=(
                        closure_err.args[0] == 'another task closed this fd'
                        or
                        closure_err.args[0] in ['another task closed this fd']
                    ),
                ) from closure_err

            # graceful EOF disconnect
            if header == b'':
                raise TransportClosed(
                    message=(
                        f'IPC transport already gracefully closed\n'
                        f')>\n'
                        f'|_{self}\n'
                    ),
                    loglevel='transport',
                    # cause=???  # handy or no?
                )

            size: int
            size, = struct.unpack("<I", header)

            log.transport(f'received header {size}')  # type: ignore
            msg_bytes: bytes = await self.recv_stream.receive_exactly(size)

            log.transport(f"received {msg_bytes}")  # type: ignore
            yield msg_bytes

    async def send(
        self,
        msg: bytes,

    ) -> None:
        '''
        Send a msgpack encoded py-object-blob-as-msg.

        '''
        async with self._send_lock:
            size: bytes = struct.pack("<I", len(msg))
            return await self.stream.send_all(size + msg)

    async def recv(self) -> Any:
        return await self._aiter_pkts.asend(None)

    async def drain(self) -> AsyncIterator[dict]:
        '''
        Drain the stream's remaining messages sent from
        the far end until the connection is closed by
        the peer.

        '''
        try:
            async for msg in self._iter_packets():
                self.drained.append(msg)
        except TransportClosed:
            for msg in self.drained:
                yield msg

    def __aiter__(self):
        return self._aiter_pkts


@acm
async def attach_to_ringbuf_stream(
    token_in: RBToken,
    token_out: RBToken,
    cleanup_in: bool = True,
    cleanup_out: bool = True
):
    '''
    Wrap a ringbuf trio.StapledStream in a MsgpackRBStream

    '''
    async with attach_to_ringbuf_pair(
        token_in,
        token_out,
        cleanup_in=cleanup_in,
        cleanup_out=cleanup_out
    ) as stream:
        yield MsgpackRBStream(stream)
