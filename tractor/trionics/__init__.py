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
    gather_contexts as gather_contexts,
    maybe_open_context as maybe_open_context,
    maybe_open_nursery as maybe_open_nursery,
)
from ._broadcast import (
    AsyncReceiver as AsyncReceiver,
    broadcast_receiver as broadcast_receiver,
    BroadcastReceiver as BroadcastReceiver,
    Lagged as Lagged,
)
from ._beg import (
    collapse_eg as collapse_eg,
    maybe_collapse_eg as maybe_collapse_eg,
    is_multi_cancelled as is_multi_cancelled,
)
from ._taskc import (
    maybe_raise_from_masking_exc as maybe_raise_from_masking_exc,
)
