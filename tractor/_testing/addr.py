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
import os
import random
from typing import (
    Type,
)
from tractor.discovery import _addr


def get_rando_addr(
    tpt_proto: str,
) -> tuple[str, str|int]:
    '''
    Used to globally override the runtime to the
    per-test-session-dynamic addr so that all tests never conflict
    with any other actor tree using the default.

    Cross-process isolation: TCP-port picks salt
    `random.randint()` with `os.getpid()` so two parallel
    pytest sessions (e.g. one running `--tpt-proto=tcp` and
    another `--tpt-proto=uds` concurrently) almost-never
    collide on the same port. Without the salt, the prior
    impl's import-time `random.randint(1000, 9999)` default
    arg was effectively a process-singleton with a 1/9000
    chance of cross-run collision per pair — and when it
    happened EVERY `reg_addr`-using test in BOTH runs would
    fight over the bind, cascading into a chain of
    "Address already in use" failures.

    For UDS this concern doesn't apply: `UDSAddress.get_random()`
    already builds socket paths from `os.getpid()` so each
    pytest process gets its own socket-path namespace.

    '''
    addr_type: Type[_addr.Addres] = _addr._address_types[tpt_proto]
    def_reg_addr: tuple[str, int] = _addr._default_lo_addrs[tpt_proto]

    # this is the "unwrapped" form expected to be passed to
    # `.open_root_actor()` by test body.
    testrun_reg_addr: tuple[str, int|str]
    match tpt_proto:
        case 'tcp':
            # Per-call randomness mixed with `os.getpid()` —
            # see the docstring above for the cross-process
            # isolation rationale. The mix means:
            # - within one pytest session, two calls return
            #   distinct ports (good for tests that need a
            #   second-different-reg-addr in one fn body, e.g.
            #   `test_tpt_bind_addrs::bind-subset-reg`),
            # - across parallel pytest sessions, the pid bias
            #   makes coincident port choices unlikely.
            port: int = 1000 + (
                random.randint(0, 8999) + os.getpid()
            ) % 9000
            testrun_reg_addr = (
                addr_type.def_bindspace,
                port,
            )

        # NOTE, file-name uniqueness (no-collisions) will be based on
        # the runtime-directory and root (pytest-proc's) pid.
        case 'uds':
            from tractor.ipc._uds import UDSAddress
            addr: UDSAddress = addr_type.get_random()
            assert addr.is_valid
            assert addr.sockpath.resolve()
            testrun_reg_addr = addr.unwrap()

    # XXX, as sanity it should never the same as the default for the
    # host-singleton registry actor.
    assert def_reg_addr != testrun_reg_addr

    return testrun_reg_addr
