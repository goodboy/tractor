'''
Basic `ActorNursery` operations and closure semantics,
- basic remote error collection,
- basic multi-subactor cancellation.

'''
# import os
# import signal
# import platform
# import time
# from itertools import repeat

import pytest
import trio
import tractor
from tractor._exceptions import ActorCancelled
# from tractor._testing import (
#     tractor_test,
# )
# from .conftest import no_windows


@pytest.mark.parametrize(
    'num_subs',
    [
        1,
        3,
    ]
)
def test_one_cancels_all(
    start_method: str,
    loglevel: str,
    debug_mode: bool,
    num_subs: int,
):
    '''
    Verify that ifa a single error bubbles to the an-scope the
    nursery will be cancelled (just like in `trio`); this is a
    one-cancels-all style strategy and are only supervision policy
    at the moment.

    '''
    async def main():
        try:
            rte = RuntimeError('Uh oh something bad in parent')
            async with tractor.open_nursery(
                start_method=start_method,
                loglevel=loglevel,
                debug_mode=debug_mode,
            ) as an:

                # spawn the same number of deamon actors which should be cancelled
                dactor_portals = []
                for i in range(num_subs):
                    name: str= f'sub_{i}'
                    ptl: tractor.Portal = await an.start_actor(
                        name=name,
                        enable_modules=[__name__],
                    )
                    dactor_portals.append(ptl)

                    # wait for booted
                    async with tractor.wait_for_actor(name):
                        print(f'{name!r} is up.')

                # simulate uncaught exc
                raise rte

            # should error here with a ``RemoteActorError`` or ``MultiError``

        except BaseExceptionGroup as _beg:
            beg = _beg

            # ?TODO? why can't we do `is` on beg?
            assert (
                beg.exceptions
                ==
                an.maybe_error.exceptions
            )

            assert len(beg.exceptions) == (
                num_subs
                +
                1  # rte from root
            )

            # all subactors should have been implicitly
            # `Portal.cancel_actor()`ed.
            excs = list(beg.exceptions)
            excs.remove(rte)
            for exc in excs:
                assert isinstance(exc, ActorCancelled)

            assert an._scope_error is rte
            assert not an._children
            assert an.cancelled is True

    trio.run(main)
