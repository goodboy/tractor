'''
Runtime boot/init sanity.

'''

import pytest
import trio

import tractor
from tractor._exceptions import RuntimeFailure


@tractor.context
async def open_new_root_in_sub(
    ctx: tractor.Context,
) -> None:

    async with tractor.open_root_actor():
        pass


@pytest.mark.parametrize(
    'open_root_in',
    ['root', 'sub'],
    ids='open_2nd_root_in={}'.format,
)
def test_only_one_root_actor(
    open_root_in: str,
    reg_addr: tuple,
    debug_mode: bool
):
    '''
    Verify we specially fail whenever more then one root actor
    is attempted to be opened within an already opened tree.

    '''
    async def main():
        async with tractor.open_nursery() as an:

            if open_root_in == 'root':
                async with tractor.open_root_actor(
                    registry_addrs=[reg_addr],
                ):
                    pass

            ptl: tractor.Portal = await an.start_actor(
                name='bad_rooty_boi',
                enable_modules=[__name__],
            )

            async with ptl.open_context(
                open_new_root_in_sub,
            ) as (ctx, first):
                pass

    if open_root_in == 'root':
        with pytest.raises(
            RuntimeFailure
        ) as excinfo:
            trio.run(main)

    else:
        with pytest.raises(
            tractor.RemoteActorError,
        ) as excinfo:
            trio.run(main)

        assert excinfo.value.boxed_type is RuntimeFailure


def test_implicit_root_via_first_nursery(
    reg_addr: tuple,
    debug_mode: bool
):
    '''
    The first `ActorNursery` open should implicitly call
    `_root.open_root_actor()`.

    '''
    async def main():
        async with tractor.open_nursery() as an:
            assert an._implicit_runtime_started
            assert tractor.current_actor().aid.name == 'root'

    trio.run(main)
