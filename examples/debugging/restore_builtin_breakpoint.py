import os
import sys

import trio
import tractor


async def main() -> None:
    async with tractor.open_nursery(debug_mode=True) as an:

        assert os.environ['PYTHONBREAKPOINT'] == 'tractor._debug._set_trace'

        # TODO: an assert that verifies the hook has indeed been, hooked
        # XD
        assert sys.breakpointhook is not tractor._debug._set_trace

        breakpoint()

    # TODO: an assert that verifies the hook is unhooked..
    assert sys.breakpointhook
    breakpoint()

if __name__ == '__main__':
    trio.run(main)
