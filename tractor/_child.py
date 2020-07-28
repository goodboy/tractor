import sys
import trio
import argparse

from ast import literal_eval

from ._actor import Actor
from ._entry import _trio_main


"""This is the "bootloader" for actors started using the native trio backend.
"""

def parse_uid(arg):
	uid = literal_eval(arg)
	assert len(uid) == 2
	assert isinstance(uid[0], str)
	assert isinstance(uid[1], str)
	return uid

def parse_ipaddr(arg):
	addr = literal_eval(arg)
	assert len(addr) == 2
	assert isinstance(addr[0], str)
	assert isinstance(addr[1], int)
	return addr


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument("--uid", type=parse_uid)
    parser.add_argument("--loglevel", type=str)
    parser.add_argument("--parent_addr", type=parse_ipaddr)

    args = parser.parse_args()

    subactor = Actor(
        args.uid[0],
        uid=args.uid[1],
        loglevel=args.loglevel,
	    spawn_method="trio"
	)

    _trio_main(
        subactor,
        ("127.0.0.1", 0),
        parent_addr=args.parent_addr
    )