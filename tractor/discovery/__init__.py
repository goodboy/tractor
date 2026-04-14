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
Discovery (protocols) API for automatic addressing
and location management of (service) actors.

NOTE: this ``__init__`` only eagerly imports the
``._multiaddr`` submodule (for public re-exports).
Heavier submodules like ``._addr`` and ``._api``
are NOT imported here to avoid circular imports;
use direct module paths for those.

'''
from ._multiaddr import (
    parse_endpoints as parse_endpoints,
    parse_maddr as parse_maddr,
    mk_maddr as mk_maddr,
)
