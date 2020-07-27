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
This is the "bootloader" for actors started using the native trio backend.

"""
import sys
import trio
import argparse

from ast import literal_eval

from ._actor import Actor
from ._entry import _trio_main


def parse_uid(arg):
    name, uuid = literal_eval(arg)  # ensure 2 elements
    return str(name), str(uuid)  # ensures str encoding

def parse_ipaddr(arg):
    host, port = literal_eval(arg)
    return (str(host), int(port))


from ._entry import _trio_main

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=parse_uid)
    parser.add_argument("--loglevel", type=str)
    parser.add_argument("--parent_addr", type=parse_ipaddr)
    parser.add_argument("--asyncio", action='store_true')
    args = parser.parse_args()

    subactor = Actor(
        args.uid[0],
        uid=args.uid[1],
        loglevel=args.loglevel,
        spawn_method="trio"
    )

    _trio_main(
        subactor,
        parent_addr=args.parent_addr,
        infect_asyncio=args.asyncio,
    )
