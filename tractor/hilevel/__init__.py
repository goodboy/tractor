# tractor: structured concurrent "actors".
# Copyright 2024-eternity Tyler Goodlet.

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
High level design patterns, APIs and runtime extensions built on top
of the `tractor` runtime core.

'''
from ._service import (
    open_service_mngr as open_service_mngr,
    get_service_mngr as get_service_mngr,
    ServiceMngr as ServiceMngr,
)
