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

    '''
    __slots__ = ()


_PROCESS_LOCAL_TOKEN: _ProcessLocalToken = _ProcessLocalToken()


class ProcessLocal(
    msgspec.Struct,
    kw_only=True,
    repr_omit_defaults=True,
):
    '''
    Generic struct marker which rejects default msgspec encoding.

    The hidden sentinel remains part of the encoded field set, so
    msgspec encounters `_ProcessLocalToken` and raises `TypeError`
    even when this value is nested inside another supported payload.
    A custom encode hook may explicitly override that safeguard.

    Keyword-only fields let subclasses add required fields after the
    marker's default sentinel.

    '''
    _process_local: _ProcessLocalToken = _PROCESS_LOCAL_TOKEN
