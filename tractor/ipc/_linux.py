
import os
import errno

import cffi
import trio

ffi = cffi.FFI()

# Declare the C functions and types we plan to use.
#    - eventfd: for creating the event file descriptor
#    - write:   for writing to the file descriptor
#    - read:    for reading from the file descriptor
#    - close:   for closing the file descriptor
ffi.cdef(
    '''
    int eventfd(unsigned int initval, int flags);

    ssize_t write(int fd, const void *buf, size_t count);
    ssize_t read(int fd, void *buf, size_t count);

    int close(int fd);
    '''
)


# Open the default dynamic library (essentially 'libc' in most cases)
C = ffi.dlopen(None)

# Constants from <sys/eventfd.h>, if needed.
EFD_SEMAPHORE = 1
EFD_CLOEXEC = 0o2000000
EFD_NONBLOCK = 0o4000


def open_eventfd(initval: int = 0, flags: int = 0) -> int:
    '''
    Open an eventfd with the given initial value and flags.
    Returns the file descriptor on success, otherwise raises OSError.

    '''
    fd = C.eventfd(initval, flags)
    if fd < 0:
        raise OSError(errno.errorcode[ffi.errno], 'eventfd failed')
    return fd

def write_eventfd(fd: int, value: int) -> int:
    '''
    Write a 64-bit integer (uint64_t) to the eventfd's counter.

    '''
    # Create a uint64_t* in C, store `value`
    data_ptr = ffi.new('uint64_t *', value)

    # Call write(fd, data_ptr, 8)
    # We expect to write exactly 8 bytes (sizeof(uint64_t))
    ret = C.write(fd, data_ptr, 8)
    if ret < 0:
        raise OSError(errno.errorcode[ffi.errno], 'write to eventfd failed')
    return ret

def read_eventfd(fd: int) -> int:
    '''
    Read a 64-bit integer (uint64_t) from the eventfd, returning the value.
    Reading resets the counter to 0 (unless using EFD_SEMAPHORE).

    '''
    # Allocate an 8-byte buffer in C for reading
    buf = ffi.new('char[]', 8)

    ret = C.read(fd, buf, 8)
    if ret < 0:
        raise OSError(errno.errorcode[ffi.errno], 'read from eventfd failed')
    # Convert the 8 bytes we read into a Python integer
    data_bytes = ffi.unpack(buf, 8)  # returns a Python bytes object of length 8
    value = int.from_bytes(data_bytes, byteorder='little', signed=False)
    return value

def close_eventfd(fd: int) -> int:
    '''
    Close the eventfd.

    '''
    ret = C.close(fd)
    if ret < 0:
        raise OSError(errno.errorcode[ffi.errno], 'close failed')


class EventFD:
    '''
    Use a previously opened eventfd(2), meant to be used in
    sub-actors after root actor opens the eventfds then passes
    them through pass_fds

    '''

    def __init__(
        self,
        fd: int,
        omode: str
    ):
        self._fd: int = fd
        self._omode: str = omode
        self._fobj = None

    @property
    def fd(self) -> int | None:
        return self._fd

    def write(self, value: int) -> int:
        return write_eventfd(self._fd, value)

    async def read(self) -> int:
        #TODO: how to handle signals?
        return await trio.to_thread.run_sync(read_eventfd, self._fd)

    def open(self):
        self._fobj = os.fdopen(self._fd, self._omode)

    def close(self):
        if self._fobj:
            self._fobj.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
