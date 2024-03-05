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
    maybe_wait_for_debugger as maybe_wait_for_debugger,
    acquire_debug_lock as acquire_debug_lock,
    breakpoint as breakpoint,
    pause as pause,
    pause_from_sync as pause_from_sync,
    shield_sigint_handler as shield_sigint_handler,
    MultiActorPdb as MultiActorPdb,
    open_crash_handler as open_crash_handler,
    maybe_open_crash_handler as maybe_open_crash_handler,
    post_mortem as post_mortem,
)
from ._stackscope import (
    enable_stack_on_sig as enable_stack_on_sig,
)
