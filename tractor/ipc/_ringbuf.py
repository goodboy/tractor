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
from multiprocessing.shared_memory import SharedMemory

import trio

from ._linux import (
    EFD_NONBLOCK,
    EventFD
)


class RingBuffSender(trio.abc.SendStream):
    '''
    IPC Reliable Ring Buffer sender side implementation

    `eventfd(2)` is used for wrap around sync, and also to signal
    writes to the reader.

    TODO: if blocked on wrap around event wait it will not respond
    to signals, fix soon TM
    '''

    def __init__(
        self,
        shm_key: str,
        write_eventfd: int,
        wrap_eventfd: int,
        start_ptr: int = 0,
        buf_size: int = 10 * 1024,
        unlink_on_exit: bool = True
    ):
        self._shm = SharedMemory(
            name=shm_key,
            size=buf_size,
            create=True
        )
        self._write_event = EventFD(write_eventfd, 'w')
        self._wrap_event = EventFD(wrap_eventfd, 'r')
        self._ptr = start_ptr
        self.unlink_on_exit = unlink_on_exit

    @property
    def key(self) -> str:
        return self._shm.name

    @property
    def size(self) -> int:
        return self._shm.size

    @property
    def ptr(self) -> int:
        return self._ptr

    @property
    def write_fd(self) -> int:
        return self._write_event.fd

    @property
    def wrap_fd(self) -> int:
        return self._wrap_event.fd

    async def send_all(self, data: bytes | bytearray | memoryview):
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

    async def aclose(self):
        self._write_event.close()
        self._wrap_event.close()
        if self.unlink_on_exit:
            self._shm.unlink()

        else:
            self._shm.close()

    async def __aenter__(self):
        self._write_event.open()
        self._wrap_event.open()
        return self


class RingBuffReceiver(trio.abc.ReceiveStream):
    '''
    IPC Reliable Ring Buffer receiver side implementation

    `eventfd(2)` is used for wrap around sync, and also to signal
    writes to the reader.

    Unless eventfd(2) object is opened with EFD_NONBLOCK flag,
    calls to `receive_some` will block the signal handling,
    on the main thread, for now solution is using polling,
    working on a way to unblock GIL during read(2) to allow
    signal processing on the main thread.
    '''

    def __init__(
        self,
        shm_key: str,
        write_eventfd: int,
        wrap_eventfd: int,
        start_ptr: int = 0,
        buf_size: int = 10 * 1024,
        flags: int = 0
    ):
        self._shm = SharedMemory(
            name=shm_key,
            size=buf_size,
            create=False
        )
        self._write_event = EventFD(write_eventfd, 'w')
        self._wrap_event = EventFD(wrap_eventfd, 'r')
        self._ptr = start_ptr
        self._flags = flags

    @property
    def key(self) -> str:
        return self._shm.name

    @property
    def size(self) -> int:
        return self._shm.size

    @property
    def ptr(self) -> int:
        return self._ptr

    @property
    def write_fd(self) -> int:
        return self._write_event.fd

    @property
    def wrap_fd(self) -> int:
        return self._wrap_event.fd

    async def receive_some(
        self,
        max_bytes: int | None = None,
        nb_timeout: float = 0.1
    ) -> memoryview:
        # if non blocking eventfd enabled, do polling
        # until next write, this allows signal handling
        if self._flags | EFD_NONBLOCK:
            delta = None
            while delta is None:
                try:
                    delta = await self._write_event.read()

                except OSError as e:
                    if e.errno == 'EAGAIN':
                        continue

                    raise e

        else:
            delta = await self._write_event.read()

        # fetch next segment and advance ptr
        next_ptr = self._ptr + delta
        segment = self._shm.buf[self._ptr:next_ptr]
        self._ptr = next_ptr

        if self.ptr == self.size:
            # reached the end, signal wrap around
            self._ptr = 0
            self._wrap_event.write(1)

        return segment

    async def aclose(self):
        self._write_event.close()
        self._wrap_event.close()
        self._shm.close()

    async def __aenter__(self):
        self._write_event.open()
        self._wrap_event.open()
        return self
