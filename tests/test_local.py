"""
Arbiter and "local" actor api
"""
import time

import pytest
import trio
import tractor


from conftest import tractor_test


@pytest.mark.trio
async def test_no_arbitter():
    """An arbitter must be established before any nurseries
    can be created.

    (In other words ``tractor.run`` must be used instead of ``trio.run`` as is
    done by the ``pytest-trio`` plugin.)
    """
    with pytest.raises(RuntimeError):
        with tractor.open_nursery():
            pass


def test_no_main():
    """An async function **must** be passed to ``tractor.run()``.
    """
    with pytest.raises(TypeError):
        tractor.run(None)


def test_local_actor_async_func(arb_addr):
    """Verify a simple async function in-process.
    """
    nums = []

    async def print_loop():
        # arbiter is started in-proc if dne
        assert tractor.current_actor().is_arbiter

        for i in range(10):
            nums.append(i)
            await trio.sleep(0.1)

    start = time.time()
    tractor.run(print_loop, arbiter_addr=arb_addr)

    # ensure the sleeps were actually awaited
    assert time.time() - start >= 1
    assert nums == list(range(10))
