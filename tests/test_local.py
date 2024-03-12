"""
Arbiter and "local" actor api
"""
import time

import pytest
import trio
import tractor

from tractor._testing import tractor_test


@pytest.mark.trio
async def test_no_runtime():
    """An arbitter must be established before any nurseries
    can be created.

    (In other words ``tractor.open_root_actor()`` must be engaged at
    some point?)
    """
    with pytest.raises(RuntimeError) :
        async with tractor.find_actor('doggy'):
            pass


@tractor_test
async def test_self_is_registered(reg_addr):
    "Verify waiting on the arbiter to register itself using the standard api."
    actor = tractor.current_actor()
    assert actor.is_arbiter
    with trio.fail_after(0.2):
        async with tractor.wait_for_actor('root') as portal:
            assert portal.channel.uid[0] == 'root'


@tractor_test
async def test_self_is_registered_localportal(reg_addr):
    "Verify waiting on the arbiter to register itself using a local portal."
    actor = tractor.current_actor()
    assert actor.is_arbiter
    async with tractor.get_arbiter(*reg_addr) as portal:
        assert isinstance(portal, tractor._portal.LocalPortal)

        with trio.fail_after(0.2):
            sockaddr = await portal.run_from_ns(
                    'self', 'wait_for_actor', name='root')
            assert sockaddr[0] == reg_addr


def test_local_actor_async_func(reg_addr):
    """Verify a simple async function in-process.
    """
    nums = []

    async def print_loop():

        async with tractor.open_root_actor(
            registry_addrs=[reg_addr],
        ):
            # arbiter is started in-proc if dne
            assert tractor.current_actor().is_arbiter

            for i in range(10):
                nums.append(i)
                await trio.sleep(0.1)

    start = time.time()
    trio.run(print_loop)

    # ensure the sleeps were actually awaited
    assert time.time() - start >= 1
    assert nums == list(range(10))
