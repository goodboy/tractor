import os
import sys

import trio
import tractor

# ensure mod-path is correct!
from tractor.devx.debug import (
    _sync_pause_from_builtin as _sync_pause_from_builtin,
)


async def main() -> None:

    # intially unset, no entry.
    orig_pybp_var: int = os.environ.get('PYTHONBREAKPOINT')
    assert orig_pybp_var in {None, "0"}

    async with tractor.open_nursery(
        debug_mode=True,
        loglevel='devx',
    ) as an:
        assert an
        assert (
            (pybp_var := os.environ['PYTHONBREAKPOINT'])
            ==
            'tractor.devx.debug._sync_pause_from_builtin'
        )

        # TODO: an assert that verifies the hook has indeed been, hooked
        # XD
        assert (
            (pybp_hook := sys.breakpointhook)
            is not tractor.devx.debug._set_trace
        )

        print(
            f'$PYTHONOBREAKPOINT: {pybp_var!r}\n'
            f'`sys.breakpointhook`: {pybp_hook!r}\n'
        )
        breakpoint()  # first bp, tractor hook set.

    # XXX AFTER EXIT (of actor-runtime) verify the hook is unset..
    #
    # YES, this is weird but it's how stdlib docs say to do it..
    # https://docs.python.org/3/library/sys.html#sys.breakpointhook
    assert os.environ.get('PYTHONBREAKPOINT') is orig_pybp_var
    assert sys.breakpointhook

    # now ensure a regular builtin pause still works
    breakpoint()  # last bp, stdlib hook restored


if __name__ == '__main__':
    trio.run(main)
