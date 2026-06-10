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
The "bootloader" for sub-actors spawned via the native `trio`
backend (the default `python -m tractor._child` CLI entry) and
the in-process `subint` backend (`tractor.spawn._subint`).

"""
from __future__ import annotations
import argparse
from ast import literal_eval
from typing import TYPE_CHECKING

from .runtime._runtime import Actor
from .spawn._entry import _trio_main

if TYPE_CHECKING:
    from .discovery._addr import UnwrappedAddress
    from .spawn._spawn import SpawnMethodKey


def parse_uid(arg):
    name, uuid = literal_eval(arg)  # ensure 2 elements
    return str(name), str(uuid)  # ensures str encoding

def parse_ipaddr(arg):
    try:
        return literal_eval(arg)

    except (ValueError, SyntaxError):
        # UDS: try to interpret as a straight up str
        return arg


def _actor_child_main(
    uid: tuple[str, str],
    loglevel: str | None,
    parent_addr: UnwrappedAddress | None,
    infect_asyncio: bool,
    spawn_method: SpawnMethodKey = 'trio',

) -> None:
    '''
    Construct the child `Actor` and dispatch to `_trio_main()`.

    Shared entry shape used by both the `python -m tractor._child`
    CLI (trio/mp subproc backends) and the `subint` backend, which
    invokes this from inside a fresh `concurrent.interpreters`
    sub-interpreter via `Interpreter.call()`.

    '''
    subactor = Actor(
        name=uid[0],
        uuid=uid[1],
        loglevel=loglevel,
        spawn_method=spawn_method,
    )
    _trio_main(
        subactor,
        parent_addr=parent_addr,
        infect_asyncio=infect_asyncio,
    )


if __name__ == "__main__":
    __tracebackhide__: bool = True

    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=parse_uid)
    parser.add_argument("--loglevel", type=str)
    parser.add_argument("--parent_addr", type=parse_ipaddr)
    parser.add_argument("--asyncio", action='store_true')
    args = parser.parse_args()

    _actor_child_main(
        uid=args.uid,
        loglevel=args.loglevel,
        parent_addr=args.parent_addr,
        infect_asyncio=args.asyncio,
        spawn_method='trio',
    )
