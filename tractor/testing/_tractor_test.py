import inspect
from functools import partial, wraps

from .. import run


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

    are defined in the `pytest` fixture space they will be automatically
    injected to tests declaring these funcargs.
    """
    @wraps(fn)
    def wrapper(*args, loglevel=None, arb_addr=None, **kwargs):
        # __tracebackhide__ = True
        if 'arb_addr' in inspect.signature(fn).parameters:
            # injects test suite fixture value to test as well
            # as `run()`
            kwargs['arb_addr'] = arb_addr
        if 'loglevel' in inspect.signature(fn).parameters:
            # allows test suites to define a 'loglevel' fixture
            # that activates the internal logging
            kwargs['loglevel'] = loglevel
        return run(
            partial(fn, *args, **kwargs),
            arbiter_addr=arb_addr, loglevel=loglevel
        )

    return wrapper
