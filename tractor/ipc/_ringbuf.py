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
from contextlib import (
    contextmanager as cm,
    asynccontextmanager as acm
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
    InternalError
)


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
    Attach a RingBuffReceiver from a previously opened
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
    Attach a RingBuffSender from a previously opened
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
async def attach_to_ringbuf_stream(
    token_in: RBToken,
    token_out: RBToken,
    cleanup_in: bool = True,
    cleanup_out: bool = True
):
    '''
    Attach a trio.StapledStream from a previously opened
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



class RingBuffBytesSender(trio.abc.SendChannel[bytes]):
    '''
    In order to guarantee full messages are received, all bytes
    sent by `RingBuffBytesSender` are preceded with a 4 byte header
    which decodes into a uint32 indicating the actual size of the
    next payload.

    '''
    def __init__(
        self,
        sender: RingBuffSender
    ):
        self._sender = sender
        self._send_lock = trio.StrictFIFOLock()

    async def send(self, value: bytes) -> None:
        async with self._send_lock:
            size: bytes = struct.pack("<I", len(value))
            return await self._sender.send_all(size + value)

    async def aclose(self) -> None:
        async with self._send_lock:
            await self._sender.aclose()


class RingBuffBytesReceiver(trio.abc.ReceiveChannel[bytes]):
    '''
    See `RingBuffBytesSender` docstring.

    A `tricycle.BufferedReceiveStream` is used for the
    `receive_exactly` API.
    '''
    def __init__(
        self,
        receiver: RingBuffReceiver
    ):
        self._receiver = receiver

    async def _receive_exactly(self, num_bytes: int) -> bytes:
        '''
        Fetch bytes from receiver until we read exactly `num_bytes`
        or end of stream is signaled.

        '''
        payload = b''
        while len(payload) < num_bytes:
            remaining = num_bytes - len(payload)

            new_bytes = await self._receiver.receive_some(
                max_bytes=remaining
            )

            if new_bytes == b'':
                raise trio.EndOfChannel

            payload += new_bytes

        return payload

    async def receive(self) -> bytes:
        header: bytes = await self._receive_exactly(4)
        size: int
        size, = struct.unpack("<I", header)
        return await self._receive_exactly(size)

    async def aclose(self) -> None:
        await self._receiver.aclose()


class RingBuffChannel(trio.abc.Channel[bytes]):
    '''
    Combine `RingBuffBytesSender` and `RingBuffBytesReceiver`
    in order to expose the bidirectional `trio.abc.Channel` API.

    '''
    def __init__(
        self,
        sender: RingBuffBytesSender,
        receiver: RingBuffBytesReceiver
    ):
        self._sender = sender
        self._receiver = receiver

    async def send(self, value: bytes):
        await self._sender.send(value)

    async def receive(self) -> bytes:
        return await self._receiver.receive()

    async def aclose(self):
        await self._receiver.aclose()
        await self._sender.aclose()


@acm
async def attach_to_ringbuf_channel(
    token_in: RBToken,
    token_out: RBToken,
    cleanup_in: bool = True,
    cleanup_out: bool = True
):
    '''
    Attach to an already opened ringbuf pair and return
    a `RingBuffChannel`.

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
        yield RingBuffChannel(
            RingBuffBytesSender(sender),
            RingBuffBytesReceiver(receiver)
        )
