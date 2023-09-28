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
Runtime "developer experience" utils and addons to aid our
(advanced) users and core devs in building distributed applications
and working with/on the actor runtime.

"""
from ._debug import (
    maybe_wait_for_debugger,
    acquire_debug_lock,
    breakpoint,
    pause,
    pause_from_sync,
    shield_sigint_handler,
    MultiActorPdb,
    open_crash_handler,
    post_mortem,
)

__all__ = [
    'maybe_wait_for_debugger',
    'acquire_debug_lock',
    'breakpoint',
    'pause',
    'pause_from_sync',
    'shield_sigint_handler',
    'MultiActorPdb',
    'open_crash_handler',
    'post_mortem',
]
