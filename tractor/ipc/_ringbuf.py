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
from contextlib import contextmanager as cm
from multiprocessing.shared_memory import SharedMemory

import trio
from msgspec import (
    Struct,
    to_builtins
)

from ._linux import (
    open_eventfd,
    close_eventfd,
    EventFD
)
from ._mp_bs import disable_mantracker
from tractor.log import get_logger


log = get_logger(__name__)


disable_mantracker()


class RBToken(Struct, frozen=True):
    '''
    RingBuffer token contains necesary info to open the two
    eventfds and the shared memory

    '''
    shm_name: str
    write_eventfd: int
    wrap_eventfd: int
    buf_size: int

    def as_msg(self):
        return to_builtins(self)

    @classmethod
    def from_msg(cls, msg: dict) -> RBToken:
        if isinstance(msg, RBToken):
            return msg

        return RBToken(**msg)


@cm
def open_ringbuf(
    shm_name: str,
    buf_size: int = 10 * 1024,
) -> RBToken:
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
            buf_size=buf_size
        )
        yield token
        close_eventfd(token.write_eventfd)
        close_eventfd(token.wrap_eventfd)

    finally:
        shm.unlink()


Buffer = bytes | bytearray | memoryview


class RingBuffSender(trio.abc.SendStream):
    '''
    IPC Reliable Ring Buffer sender side implementation

    `eventfd(2)` is used for wrap around sync, and also to signal
    writes to the reader.

    '''
    def __init__(
        self,
        token: RBToken,
        start_ptr: int = 0,
        is_ipc: bool = True
    ):
        self._token = RBToken.from_msg(token)
        self._shm: SharedMemory | None = None
        self._write_event = EventFD(self._token.write_eventfd, 'w')
        self._wrap_event = EventFD(self._token.wrap_eventfd, 'r')
        self._ptr = start_ptr

        self._is_ipc = is_ipc
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

    async def aclose(self):
        if self._is_ipc:
            self._write_event.close()
            self._wrap_event.close()
            self._shm.close()

    async def __aenter__(self):
        self.open()
        return self


class RingBuffReceiver(trio.abc.ReceiveStream):
    '''
    IPC Reliable Ring Buffer receiver side implementation

    `eventfd(2)` is used for wrap around sync, and also to signal
    writes to the reader.

    '''
    def __init__(
        self,
        token: RBToken,
        start_ptr: int = 0,
        is_ipc: bool = True
    ):
        self._token = RBToken.from_msg(token)
        self._shm: SharedMemory | None = None
        self._write_event = EventFD(self._token.write_eventfd, 'w')
        self._wrap_event = EventFD(self._token.wrap_eventfd, 'r')
        self._ptr = start_ptr
        self._write_ptr = start_ptr
        self._is_ipc = is_ipc

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

    async def receive_some(self, max_bytes: int | None = None) -> memoryview:
        delta = self._write_ptr - self._ptr
        if delta == 0:
            delta = await self._write_event.read()
            self._write_ptr += delta

        if isinstance(max_bytes, int):
            if max_bytes == 0:
                raise ValueError('if set, max_bytes must be > 0')
            delta = min(delta, max_bytes)

        target_ptr = self._ptr + delta

        # fetch next segment and advance ptr
        segment = self._shm.buf[self._ptr:target_ptr]
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

    async def aclose(self):
        if self._is_ipc:
            self._write_event.close()
            self._wrap_event.close()
            self._shm.close()

    async def __aenter__(self):
        self.open()
        return self
