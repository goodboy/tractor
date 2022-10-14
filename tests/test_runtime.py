"""
Verifying internal runtime state and undocumented extras.

"""
import os

import pytest
import trio
import tractor

from conftest import tractor_test


_file_path: str = ''


def unlink_file():
    print('Removing tmp file!')
    os.remove(_file_path)


async def crash_and_clean_tmpdir(
    tmp_file_path: str,
    error: bool = True,
):
    global _file_path
    _file_path = tmp_file_path

    actor = tractor.current_actor()
    actor.lifetime_stack.callback(unlink_file)

    assert os.path.isfile(tmp_file_path)
    await trio.sleep(0.1)
    if error:
        assert 0
    else:
        actor.cancel_soon()


@pytest.mark.parametrize(
    'error_in_child',
    [True, False],
)
@tractor_test
async def test_lifetime_stack_wipes_tmpfile(
    tmp_path,
    error_in_child: bool,
):
    child_tmp_file = tmp_path / "child.txt"
    child_tmp_file.touch()
    assert child_tmp_file.exists()
    path = str(child_tmp_file)

    try:
        with trio.move_on_after(0.5):
            async with tractor.open_nursery() as n:
                    await (  # inlined portal
                        await n.run_in_actor(
                            crash_and_clean_tmpdir,
                            tmp_file_path=path,
                            error=error_in_child,
                        )
                    ).result()

    except (
        tractor.RemoteActorError,
        tractor.BaseExceptionGroup,
    ):
        pass

    # tmp file should have been wiped by
    # teardown stack.
    assert not child_tmp_file.exists()
