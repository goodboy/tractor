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

NOTE: this ``__init__`` only eagerly imports the lightweight
``._multiaddr`` and ``._tunnel`` submodules for public re-exports.
Heavier submodules like ``._addr`` and ``._api`` are NOT imported
here to avoid circular imports; use direct module paths for those.

'''
from ._bindspace import (
    BindspaceHandle as BindspaceHandle,
    BindspaceIdentity as BindspaceIdentity,
    BindspaceKind as BindspaceKind,
    BindspaceOwnership as BindspaceOwnership,
    BindspaceSpec as BindspaceSpec,
    CURRENT_NETNS as CURRENT_NETNS,
    attach_netns as attach_netns,
    open_netns as open_netns,
)
from ._multiaddr import (
    parse_endpoints as parse_endpoints,
    parse_maddr as parse_maddr,
    mk_maddr as mk_maddr,
)
from ._tunnel import (
    TunnelledAddress as TunnelledAddress,
    TunnelSpec as TunnelSpec,
    WGTunnelSpec as WGTunnelSpec,
    mb_pubkey as mb_pubkey,
    mk_wg_maddr as mk_wg_maddr,
    parse_wg_maddr as parse_wg_maddr,
    read_wg_peers as read_wg_peers,
    read_wg_pubkey as read_wg_pubkey,
    strip_tunnels as strip_tunnels,
    tunnels_of as tunnels_of,
    verify_wg_peer as verify_wg_peer,
    wg8_pubkey as wg8_pubkey,
)
