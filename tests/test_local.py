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


@tractor_test
async def test_self_is_registered():
    "Verify waiting on the arbiter to register itself using the standard api."
    actor = tractor.current_actor()
    assert actor.is_arbiter
    async with tractor.wait_for_actor('arbiter') as portal:
        assert portal.channel.uid[0] == 'arbiter'


@tractor_test
async def test_self_is_registered_localportal(arb_addr):
    "Verify waiting on the arbiter to register itself using a local portal."
    actor = tractor.current_actor()
    assert actor.is_arbiter
    async with tractor.get_arbiter(*arb_addr) as portal:
        assert isinstance(portal, tractor._portal.LocalPortal)
        sockaddr = await portal.run('self', 'wait_for_actor', name='arbiter')
        assert sockaddr[0] == arb_addr


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
