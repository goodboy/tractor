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

import inspect
import platform
from functools import partial, wraps

import trio
import tractor


__all__ = ['tractor_test']


def tractor_test(fn):
    """
    Use:

    @tractor_test
    async def test_whatever():
        await ...

    If fixtures:

        - ``arb_addr`` (a socket addr tuple where arbiter is listening)
        - ``loglevel`` (logging level passed to tractor internals)
        - ``start_method`` (subprocess spawning backend)

    are defined in the `pytest` fixture space they will be automatically
    injected to tests declaring these funcargs.
    """
    @wraps(fn)
    def wrapper(
        *args,
        loglevel=None,
        arb_addr=None,
        start_method=None,
        **kwargs
    ):
        # __tracebackhide__ = True

        if 'arb_addr' in inspect.signature(fn).parameters:
            # injects test suite fixture value to test as well
            # as `run()`
            kwargs['arb_addr'] = arb_addr

        if 'loglevel' in inspect.signature(fn).parameters:
            # allows test suites to define a 'loglevel' fixture
            # that activates the internal logging
            kwargs['loglevel'] = loglevel

        if start_method is None:
            if platform.system() == "Windows":
                start_method = 'spawn'
            else:
                start_method = 'trio'

        if 'start_method' in inspect.signature(fn).parameters:
            # set of subprocess spawning backends
            kwargs['start_method'] = start_method

        if kwargs:

            # use explicit root actor start

            async def _main():
                async with tractor.open_root_actor(
                    # **kwargs,
                    arbiter_addr=arb_addr,
                    loglevel=loglevel,
                    start_method=start_method,

                    # TODO: only enable when pytest is passed --pdb
                    # debug_mode=True,

                ) as actor:
                    await fn(*args, **kwargs)

            main = _main

        else:
            # use implicit root actor start
            main = partial(fn, *args, **kwargs)

        return trio.run(main)
            # arbiter_addr=arb_addr,
            # loglevel=loglevel,
            # start_method=start_method,
        # )

    return wrapper
