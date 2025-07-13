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
Random IPC addr generation for isolating
the discovery space between test sessions.

Might be eventually useful to expose as a util set from
our `tractor.discovery` subsys?

'''
import random
from typing import (
    Type,
)
from tractor import (
    _addr,
)


def get_rando_addr(
    tpt_proto: str,
    *,

    # choose random port at import time
    _rando_port: str = random.randint(1000, 9999)

) -> tuple[str, str|int]:
    '''
    Used to globally override the runtime to the
    per-test-session-dynamic addr so that all tests never conflict
    with any other actor tree using the default.

    '''
    addr_type: Type[_addr.Addres] = _addr._address_types[tpt_proto]
    def_reg_addr: tuple[str, int] = _addr._default_lo_addrs[tpt_proto]

    # this is the "unwrapped" form expected to be passed to
    # `.open_root_actor()` by test body.
    testrun_reg_addr: tuple[str, int|str]
    match tpt_proto:
        case 'tcp':
            testrun_reg_addr = (
                addr_type.def_bindspace,
                _rando_port,
            )

        # NOTE, file-name uniqueness (no-collisions) will be based on
        # the runtime-directory and root (pytest-proc's) pid.
        case 'uds':
            testrun_reg_addr = addr_type.get_random().unwrap()

    # XXX, as sanity it should never the same as the default for the
    # host-singleton registry actor.
    assert def_reg_addr != testrun_reg_addr

    return testrun_reg_addr
