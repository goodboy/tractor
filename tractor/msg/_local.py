# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public License
# as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
'''
Markers for process-local values which must not cross actor IPC.

'''
from __future__ import annotations

import msgspec


class _ProcessLocalToken:
    '''
    Unsupported msgspec value embedded in every `ProcessLocal`.

    A sentinel is one private, unique object used as an identity
    marker instead of application data. Every `ProcessLocal` holds
    the same `_PROCESS_LOCAL_TOKEN` instance so construction can
    verify it by identity and msgspec must encounter its unsupported
    type on encode.

    '''
    __slots__ = ()


_PROCESS_LOCAL_TOKEN: _ProcessLocalToken = _ProcessLocalToken()


class ProcessLocal(
    msgspec.Struct,
    kw_only=True,
    repr_omit_defaults=True,
):
    '''
    Struct whose `_process_local` field blocks msgspec encoding.

    `_process_local` must remain the unsupported singleton
    `_PROCESS_LOCAL_TOKEN`. Msgspec reaches that field during direct
    or nested encoding and raises `TypeError`; replacing it with an
    encodable value or omitting its default would bypass the guard.
    A custom encode hook may still explicitly override the safeguard.

    Construction therefore rejects a replacement sentinel and any
    subclass configured with `omit_defaults=True`. A subclass
    `__post_init__()` is wrapped with checks before and after its
    body, preventing that hook from skipping or later replacing the
    sentinel.

    Keyword-only fields let subclasses add required fields after the
    marker's default sentinel.

    '''
    _process_local: _ProcessLocalToken = _PROCESS_LOCAL_TOKEN

    def __init_subclass__(cls, **kwargs: object) -> None:
        '''
        Check the sentinel around a subclass post-init hook.

        '''
        super().__init_subclass__(**kwargs)
        subclass_post_init = cls.__dict__.get('__post_init__')
        if subclass_post_init is None:
            return

        def checked_post_init(self: ProcessLocal) -> None:
            ProcessLocal.__post_init__(self)
            subclass_post_init(self)
            ProcessLocal.__post_init__(self)

        cls.__post_init__ = checked_post_init

    def __post_init__(self) -> None:
        '''
        Require the singleton sentinel and forbid default omission.

        '''
        if self._process_local is not _PROCESS_LOCAL_TOKEN:
            raise TypeError(
                '`ProcessLocal._process_local` must retain its '
                'internal sentinel'
            )

        if self.__struct_config__.omit_defaults:
            raise TypeError(
                '`ProcessLocal` subclasses may not enable '
                '`omit_defaults`'
            )


class FrozenProcessLocal(
    ProcessLocal,
    frozen=True,
):
    '''
    Frozen `ProcessLocal` whose struct fields reject reassignment.

    The inherited `_process_local` sentinel still blocks direct and
    nested encoding. Freezing is shallow: struct fields cannot be
    replaced, while mutable objects referenced by those fields retain
    their own mutation semantics.

    '''
