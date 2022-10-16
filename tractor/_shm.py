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

"""
SC friendly shared memory management geared at real-time
processing.

Support for ``numpy`` compatible array-buffers is provided but is
considered optional within the context of this runtime-library.

"""
from __future__ import annotations
from sys import byteorder
import time
from typing import Optional
from multiprocessing.shared_memory import (
    SharedMemory,
    ShareableList,
    _USE_POSIX,
)

if _USE_POSIX:
    from _posixshmem import shm_unlink

from msgspec import Struct
import numpy as np
from numpy.lib import recfunctions as rfn
import tractor

from .log import get_logger


log = get_logger(__name__)


# how  much is probably dependent on lifestyle
_secs_in_day = int(60 * 60 * 24)
# we try for a buncha times, but only on a run-every-other-day kinda week.
_days_worth = 16
_default_size = _days_worth * _secs_in_day
# where to start the new data append index
_rt_buffer_start = int((_days_worth - 1) * _secs_in_day)


def disable_mantracker():
    '''
    Disable all ``multiprocessing``` "resource tracking" machinery since
    it's an absolute multi-threaded mess of non-SC madness.

    '''
    from multiprocessing import resource_tracker as mantracker

    # Tell the "resource tracker" thing to fuck off.
    class ManTracker(mantracker.ResourceTracker):
        def register(self, name, rtype):
            pass

        def unregister(self, name, rtype):
            pass

        def ensure_running(self):
            pass

    # "know your land and know your prey"
    # https://www.dailymotion.com/video/x6ozzco
    mantracker._resource_tracker = ManTracker()
    mantracker.register = mantracker._resource_tracker.register
    mantracker.ensure_running = mantracker._resource_tracker.ensure_running
    # ensure_running = mantracker._resource_tracker.ensure_running
    mantracker.unregister = mantracker._resource_tracker.unregister
    mantracker.getfd = mantracker._resource_tracker.getfd


disable_mantracker()


class SharedInt:
    '''
    Wrapper around a single entry shared memory array which
    holds an ``int`` value used as an index counter.

    '''
    def __init__(
        self,
        shm: SharedMemory,
    ) -> None:
        self._shm = shm

    @property
    def value(self) -> int:
        return int.from_bytes(self._shm.buf, byteorder)

    @value.setter
    def value(self, value) -> None:
        self._shm.buf[:] = value.to_bytes(self._shm.size, byteorder)

    def destroy(self) -> None:
        if _USE_POSIX:
            # We manually unlink to bypass all the "resource tracker"
            # nonsense meant for non-SC systems.
            name = self._shm.name
            try:
                shm_unlink(name)
            except FileNotFoundError:
                # might be a teardown race here?
                log.warning(f'Shm for {name} already unlinked?')


class _NpToken(Struct, frozen=True):
    '''
    Internal represenation of a shared memory ``numpy`` array "token"
    which can be used to key and load a system (OS) wide shm entry
    and correctly read the array by type signature.

    This type is msg safe.

    '''
    shm_name: str  # this servers as a "key" value
    shm_first_index_name: str
    shm_last_index_name: str
    dtype_descr: tuple
    size: int  # in struct-array index / row terms

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(list(map(tuple, self.dtype_descr))).descr

    def as_msg(self):
        return self.to_dict()

    @classmethod
    def from_msg(cls, msg: dict) -> _NpToken:
        if isinstance(msg, _NpToken):
            return msg

        # TODO: native struct decoding
        # return _token_dec.decode(msg)

        msg['dtype_descr'] = tuple(map(tuple, msg['dtype_descr']))
        return _NpToken(**msg)


# _token_dec = msgspec.msgpack.Decoder(_NpToken)

# TODO: this api?
# _known_tokens = tractor.ActorVar('_shm_tokens', {})
# _known_tokens = tractor.ContextStack('_known_tokens', )
# _known_tokens = trio.RunVar('shms', {})

# process-local store of keys to tokens
_known_tokens = {}


def get_shm_token(key: str) -> _NpToken | str:
    '''
    Convenience func to check if a token
    for the provided key is known by this process.

    Returns either the ``numpy`` token or a string for a shared list.

    '''
    return _known_tokens.get(key)


def _make_token(
    key: str,
    size: int,
    dtype: np.dtype,

) -> _NpToken:
    '''
    Create a serializable token that can be used
    to access a shared array.

    '''
    return _NpToken(
        shm_name=key,
        shm_first_index_name=key + "_first",
        shm_last_index_name=key + "_last",
        dtype_descr=tuple(np.dtype(dtype).descr),
        size=size,
    )


class ShmArray:
    '''
    A shared memory ``numpy.ndarray`` API.

    An underlying shared memory buffer is allocated based on
    a user specified ``numpy.ndarray``. This fixed size array
    can be read and written to by pushing data both onto the "front"
    or "back" of a set index range. The indexes for the "first" and
    "last" index are themselves stored in shared memory (accessed via
    ``SharedInt`` interfaces) values such that multiple processes can
    interact with the same array using a synchronized-index.

    '''
    def __init__(
        self,
        shmarr: np.ndarray,
        first: SharedInt,
        last: SharedInt,
        shm: SharedMemory,
        # readonly: bool = True,
    ) -> None:
        self._array = shmarr

        # indexes for first and last indices corresponding
        # to fille data
        self._first = first
        self._last = last

        self._len = len(shmarr)
        self._shm = shm
        self._post_init: bool = False

        # pushing data does not write the index (aka primary key)
        dtype = shmarr.dtype
        if dtype.fields:
            self._write_fields = list(shmarr.dtype.fields.keys())[1:]
        else:
            self._write_fields = None

    # TODO: ringbuf api?

    @property
    def _token(self) -> _NpToken:
        return _NpToken(
            shm_name=self._shm.name,
            shm_first_index_name=self._first._shm.name,
            shm_last_index_name=self._last._shm.name,
            dtype_descr=tuple(self._array.dtype.descr),
            size=self._len,
        )

    @property
    def token(self) -> dict:
        """Shared memory token that can be serialized and used by
        another process to attach to this array.
        """
        return self._token.as_msg()

    @property
    def index(self) -> int:
        return self._last.value % self._len

    @property
    def array(self) -> np.ndarray:
        '''
        Return an up-to-date ``np.ndarray`` view of the
        so-far-written data to the underlying shm buffer.

        '''
        a = self._array[self._first.value:self._last.value]

        # first, last = self._first.value, self._last.value
        # a = self._array[first:last]

        # TODO: eventually comment this once we've not seen it in the
        # wild in a long time..
        # XXX: race where first/last indexes cause a reader
        # to load an empty array..
        if len(a) == 0 and self._post_init:
            raise RuntimeError('Empty array race condition hit!?')
            # breakpoint()

        return a

    def ustruct(
        self,
        fields: Optional[list[str]] = None,

        # type that all field values will be cast to
        # in the returned view.
        common_dtype: np.dtype = np.float,

    ) -> np.ndarray:

        array = self._array

        if fields:
            selection = array[fields]
            # fcount = len(fields)
        else:
            selection = array
            # fcount = len(array.dtype.fields)

        # XXX: manual ``.view()`` attempt that also doesn't work.
        # uview = selection.view(
        #     dtype='<f16',
        # ).reshape(-1, 4, order='A')

        # assert len(selection) == len(uview)

        u = rfn.structured_to_unstructured(
            selection,
            # dtype=float,
            copy=True,
        )

        # unstruct = np.ndarray(u.shape, dtype=a.dtype, buffer=shm.buf)
        # array[:] = a[:]
        return u
        # return ShmArray(
        #     shmarr=u,
        #     first=self._first,
        #     last=self._last,
        #     shm=self._shm
        # )

    def last(
        self,
        length: int = 1,

    ) -> np.ndarray:
        '''
        Return the last ``length``'s worth of ("row") entries from the
        array.

        '''
        return self.array[-length:]

    def push(
        self,
        data: np.ndarray,

        field_map: Optional[dict[str, str]] = None,
        prepend: bool = False,
        update_first: bool = True,
        start: Optional[int] = None,

    ) -> int:
        '''
        Ring buffer like "push" to append data
        into the buffer and return updated "last" index.

        NB: no actual ring logic yet to give a "loop around" on overflow
        condition, lel.

        '''
        length = len(data)

        if prepend:
            index = (start or self._first.value) - length

            if index < 0:
                raise ValueError(
                    f'Array size of {self._len} was overrun during prepend.\n'
                    f'You have passed {abs(index)} too many datums.'
                )

        else:
            index = start if start is not None else self._last.value

        end = index + length

        if field_map:
            src_names, dst_names = zip(*field_map.items())
        else:
            dst_names = src_names = self._write_fields

        try:
            self._array[
                list(dst_names)
            ][index:end] = data[list(src_names)][:]

            # NOTE: there was a race here between updating
            # the first and last indices and when the next reader
            # tries to access ``.array`` (which due to the index
            # overlap will be empty). Pretty sure we've fixed it now
            # but leaving this here as a reminder.
            if prepend and update_first and length:
                assert index < self._first.value

            if (
                index < self._first.value
                and update_first
            ):
                assert prepend, 'prepend=True not passed but index decreased?'
                self._first.value = index

            elif not prepend:
                self._last.value = end

            self._post_init = True
            return end

        except ValueError as err:
            if field_map:
                raise

            # should raise if diff detected
            self.diff_err_fields(data)
            raise err

    def diff_err_fields(
        self,
        data: np.ndarray,
    ) -> None:
        # reraise with any field discrepancy
        our_fields, their_fields = (
            set(self._array.dtype.fields),
            set(data.dtype.fields),
        )

        only_in_ours = our_fields - their_fields
        only_in_theirs = their_fields - our_fields

        if only_in_ours:
            raise TypeError(
                f"Input array is missing field(s): {only_in_ours}"
            )
        elif only_in_theirs:
            raise TypeError(
                f"Input array has unknown field(s): {only_in_theirs}"
            )

    # TODO: support "silent" prepends that don't update ._first.value?
    def prepend(
        self,
        data: np.ndarray,
    ) -> int:
        end = self.push(data, prepend=True)
        assert end

    def close(self) -> None:
        self._first._shm.close()
        self._last._shm.close()
        self._shm.close()

    def destroy(self) -> None:
        if _USE_POSIX:
            # We manually unlink to bypass all the "resource tracker"
            # nonsense meant for non-SC systems.
            shm_unlink(self._shm.name)

        self._first.destroy()
        self._last.destroy()

    def flush(self) -> None:
        # TODO: flush to storage backend like markestore?
        ...


def open_shm_ndarray(

    key: Optional[str] = None,
    size: int = int(2 ** 10),
    dtype: np.dtype | None = None,
    append_start_index: int = 0,
    readonly: bool = False,

) -> ShmArray:
    '''
    Open a memory shared ``numpy`` using the standard library.

    This call unlinks (aka permanently destroys) the buffer on teardown
    and thus should be used from the parent-most accessor (process).

    '''
    # create new shared mem segment for which we
    # have write permission
    a = np.zeros(size, dtype=dtype)
    a['index'] = np.arange(len(a))

    shm = SharedMemory(
        name=key,
        create=True,
        size=a.nbytes
    )
    array = np.ndarray(
        a.shape,
        dtype=a.dtype,
        buffer=shm.buf
    )
    array[:] = a[:]
    array.setflags(write=int(not readonly))

    token = _make_token(
        key=key,
        size=size,
        dtype=dtype,
    )

    # create single entry arrays for storing an first and last indices
    first = SharedInt(
        shm=SharedMemory(
            name=token.shm_first_index_name,
            create=True,
            size=4,  # std int
        )
    )

    last = SharedInt(
        shm=SharedMemory(
            name=token.shm_last_index_name,
            create=True,
            size=4,  # std int
        )
    )

    # Start the "real-time" append-updated (or "pushed-to") section
    # after some start index: ``append_start_index``. This allows appending
    # from a start point in the array which isn't the 0 index and looks
    # something like,
    # -------------------------
    # |              |        i
    # _________________________
    # <-------------> <------->
    #  history         real-time
    #
    # Once fully "prepended", the history section will leave the
    # ``ShmArray._start.value: int = 0`` and the yet-to-be written
    # real-time section will start at ``ShmArray.index: int``.

    # this sets the index to 3/4 of the length of the buffer
    # leaving a "days worth of second samples" for the real-time
    # section.
    last.value = first.value = append_start_index

    shmarr = ShmArray(
        array,
        first,
        last,
        shm,
    )

    assert shmarr._token == token
    _known_tokens[key] = shmarr.token

    # "unlink" created shm on process teardown by
    # pushing teardown calls onto actor context stack

    stack = tractor.current_actor().lifetime_stack
    stack.callback(shmarr.close)
    stack.callback(shmarr.destroy)

    return shmarr


def attach_shm_ndarray(
    token: tuple[str, str, tuple[str, str]],
    readonly: bool = True,

) -> ShmArray:
    '''
    Attach to an existing shared memory array previously
    created by another process using ``open_shared_array``.

    No new shared mem is allocated but wrapper types for read/write
    access are constructed.

    '''
    token = _NpToken.from_msg(token)
    key = token.shm_name

    if key in _known_tokens:
        assert _NpToken.from_msg(_known_tokens[key]) == token, "WTF"

    # XXX: ugh, looks like due to the ``shm_open()`` C api we can't
    # actually place files in a subdir, see discussion here:
    # https://stackoverflow.com/a/11103289

    # attach to array buffer and view as per dtype
    _err: Optional[Exception] = None
    for _ in range(3):
        try:
            shm = SharedMemory(
                name=key,
                create=False,
            )
            break
        except OSError as oserr:
            _err = oserr
            time.sleep(0.1)
    else:
        if _err:
            raise _err

    shmarr = np.ndarray(
        (token.size,),
        dtype=token.dtype,
        buffer=shm.buf
    )
    shmarr.setflags(write=int(not readonly))

    first = SharedInt(
        shm=SharedMemory(
            name=token.shm_first_index_name,
            create=False,
            size=4,  # std int
        ),
    )
    last = SharedInt(
        shm=SharedMemory(
            name=token.shm_last_index_name,
            create=False,
            size=4,  # std int
        ),
    )

    # make sure we can read
    first.value

    sha = ShmArray(
        shmarr,
        first,
        last,
        shm,
    )
    # read test
    sha.array

    # Stash key -> token knowledge for future queries
    # via `maybe_opepn_shm_array()` but only after we know
    # we can attach.
    if key not in _known_tokens:
        _known_tokens[key] = token

    # "close" attached shm on actor teardown
    tractor.current_actor().lifetime_stack.callback(sha.close)

    return sha


def maybe_open_shm_ndarray(
    key: str,  # unique identifier for segment

    # from ``open_shm_array()``
    size: int = int(2 ** 10),  # array length in index terms
    dtype: np.dtype | None = None,
    append_start_index: int = 0,
    readonly: bool = True,

) -> tuple[ShmArray, bool]:
    '''
    Attempt to attach to a shared memory block using a "key" lookup
    to registered blocks in the users overall "system" registry
    (presumes you don't have the block's explicit token).

    This function is meant to solve the problem of discovering whether
    a shared array token has been allocated or discovered by the actor
    running in **this** process. Systems where multiple actors may seek
    to access a common block can use this function to attempt to acquire
    a token as discovered by the actors who have previously stored
    a "key" -> ``_NpToken`` map in an actor local (aka python global)
    variable.

    If you know the explicit ``_NpToken`` for your memory segment instead
    use ``attach_shm_array``.

    '''
    try:
        # see if we already know this key
        token = _known_tokens[key]
        return (
            attach_shm_ndarray(
                token=token,
                readonly=readonly,
            ),
            False,  # not newly opened
        )
    except KeyError:
        log.warning(f"Could not find {key} in shms cache")
        if dtype:
            token = _make_token(
                key,
                size=size,
                dtype=dtype,
            )
        else:

            try:
                return (
                    attach_shm_ndarray(
                        token=token,
                        readonly=readonly,
                    ),
                    False,
                )
            except FileNotFoundError:
                log.warning(f"Could not attach to shm with token {token}")

        # This actor does not know about memory
        # associated with the provided "key".
        # Attempt to open a block and expect
        # to fail if a block has been allocated
        # on the OS by someone else.
        return (
            open_shm_ndarray(
                key=key,
                size=size,
                dtype=dtype,
                append_start_index=append_start_index,
                readonly=readonly,
            ),
            True,
        )


class ShmList(ShareableList):
    '''
    Carbon copy of ``.shared_memory.ShareableList`` but add a
    readonly state instance var.

    '''
    def __init__(
        self,
        sequence: list | None = None,
        *,
        name: str | None = None,
        readonly: bool = True

    ) -> None:
        self._readonly = readonly
        self._key = name
        return super().__init__(
            sequence=sequence,
            name=name,
        )

    @property
    def key(self) -> str:
        return self._key

    def __setitem__(
        self,
        position,
        value,

    ) -> None:

        # mimick ``numpy`` error
        if self._readonly:
            raise ValueError('assignment destination is read-only')

        return super().__setitem__(position, value)


def open_shm_list(
    key: str,
    sequence: list | None = None,
    size: int = int(2 ** 10),
    dtype: np.dtype | None = None,
    readonly: bool = True,

) -> ShmList:

    if sequence is None:
        sequence = list(map(float, range(size)))

    shml = ShmList(
        sequence=sequence,
        name=key,
        readonly=readonly,
    )

    # "close" attached shm on actor teardown
    tractor.current_actor().lifetime_stack.callback(shml.shm.close)
    tractor.current_actor().lifetime_stack.callback(shml.shm.unlink)

    return shml


def attach_shm_list(
    key: str,
) -> ShmList:

    return ShmList(name=key)
