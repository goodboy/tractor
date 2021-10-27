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
Sugary patterns for trio + tractor designs.

'''
from ._mngrs import (
    gather_contexts,
    maybe_open_context,
)
from ._broadcast import (
    broadcast_receiver,
    BroadcastReceiver,
    Lagged,
)


__all__ = [
    'gather_contexts',
    'broadcast_receiver',
    'BroadcastReceiver',
    'Lagged',
    'maybe_open_context',
]
