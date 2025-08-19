'''
Unit-ish tests for specific IPC transport protocol backends.

'''
from __future__ import annotations
from pathlib import Path

import pytest
import trio
import tractor
from tractor import (
    Actor,
    _state,
    _addr,
)


@pytest.fixture
def bindspace_dir_str() -> str:

    rt_dir: Path = tractor._state.get_rt_dir()
    bs_dir: Path = rt_dir / 'doggy'
    bs_dir_str: str = str(bs_dir)
    assert not bs_dir.is_dir()

    yield bs_dir_str

    # delete it on suite teardown.
    # ?TODO? should we support this internally
    # or is leaking it ok?
    if bs_dir.is_dir():
        bs_dir.rmdir()


def test_uds_bindspace_created_implicitly(
    debug_mode: bool,
    bindspace_dir_str: str,
):
    registry_addr: tuple = (
        f'{bindspace_dir_str}',
        'registry@doggy.sock',
    )
    bs_dir_str: str = registry_addr[0]

    # XXX, ensure bindspace-dir DNE beforehand!
    assert not Path(bs_dir_str).is_dir()

    async def main():
        async with tractor.open_nursery(
            enable_transports=['uds'],
            registry_addrs=[registry_addr],
            debug_mode=debug_mode,
        ) as _an:

            # XXX MUST be created implicitly by
            # `.ipc._uds.start_listener()`!
            assert Path(bs_dir_str).is_dir()

            root: Actor = tractor.current_actor()
            assert root.is_registrar

            assert registry_addr in root.reg_addrs
            assert (
                registry_addr
                in
                _state._runtime_vars['_registry_addrs']
            )
            assert (
                _addr.wrap_address(registry_addr)
                in
                root.registry_addrs
            )

    trio.run(main)


def test_uds_double_listen_raises_connerr(
    debug_mode: bool,
    bindspace_dir_str: str,
):
    registry_addr: tuple = (
        f'{bindspace_dir_str}',
        'registry@doggy.sock',
    )

    async def main():
        async with tractor.open_nursery(
            enable_transports=['uds'],
            registry_addrs=[registry_addr],
            debug_mode=debug_mode,
        ) as _an:

            # runtime up
            root: Actor = tractor.current_actor()

            from tractor.ipc._uds import (
                start_listener,
                UDSAddress,
            )
            ya_bound_addr: UDSAddress = root.registry_addrs[0]
            try:
                await start_listener(
                    addr=ya_bound_addr,
                )
            except ConnectionError as connerr:
                assert type(src_exc := connerr.__context__) is OSError
                assert 'Address already in use' in src_exc.args
                # complete, exit test.

            else:
                pytest.fail('It dint raise a connerr !?')


    trio.run(main)
