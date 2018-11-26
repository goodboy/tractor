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

    If an ``arb_addr`` (a socket addr tuple) is defined in the
    `pytest` fixture space it will be automatically injected.
    """
    @wraps(fn)
    def wrapper(*args, arb_addr=None, **kwargs):
        # __tracebackhide__ = True
        if 'arb_addr' in inspect.signature(fn).parameters:
            kwargs['arb_addr'] = arb_addr
        return run(
            partial(fn, *args, **kwargs), arbiter_addr=arb_addr)

    return wrapper
