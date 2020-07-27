import sys
import trio
import argparse

from ._actor import Actor
from ._entry import _trio_main


"""This is the "bootloader" for actors started using the native trio backend
added in #128
"""


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument("name")
    parser.add_argument("uid")
    parser.add_argument("loglevel")
    parser.add_argument("bind_addr")
    parser.add_argument("parent_addr")
    parser.add_argument("arbiter_addr")

    args = parser.parse_args()

    bind_addr = args.bind_addr.split(":")
    bind_addr = (bind_addr[0], int(bind_addr[1]))
    
    parent_addr = args.parent_addr.split(":")
    parent_addr = (parent_addr[0], int(parent_addr[1]))

    arbiter_addr = args.arbiter_addr.split(":")
    arbiter_addr = (arbiter_addr[0], int(arbiter_addr[1]))

    if args.loglevel == "None":
        loglevel = None
    else:
        loglevel = args.loglevel

    subactor = Actor(
        args.name,
        uid=args.uid,
        loglevel=loglevel,
        arbiter_addr=arbiter_addr
    )
    subactor._spawn_method = "trio"

    _trio_main(
        subactor,
        bind_addr,
        parent_addr=parent_addr
    )