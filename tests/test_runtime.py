"""
Verifying internal runtime state and undocumented extras.

"""
import os

import pytest
import trio
import tractor

from tractor._testing import tractor_test


_file_path: str = ''


def unlink_file():
    print('Removing tmp file!')
    os.remove(_file_path)


async def crash_and_clean_tmpdir(
    tmp_file_path: str,
    error: bool = True,
    rent_cancel: bool = True,

    # XXX unused, but do we really need to test these cases?
    self_cancel: bool = False,
):
    global _file_path
    _file_path = tmp_file_path

    actor = tractor.current_actor()
    actor.lifetime_stack.callback(unlink_file)

    assert os.path.isfile(tmp_file_path)
    await trio.sleep(0.1)
    if error:
        print('erroring in subactor!')
        assert 0

    elif self_cancel:
        print('SELF-cancelling subactor!')
        actor.cancel_soon()

    elif rent_cancel:
        await trio.sleep_forever()

    print('subactor exiting task!')


@pytest.mark.parametrize(
    'error_in_child',
    [True, False],
    ids='error_in_child={}'.format,
)
@tractor_test
async def test_lifetime_stack_wipes_tmpfile(
    tmp_path,
    error_in_child: bool,
    loglevel: str,
    # log: tractor.log.StackLevelAdapter,
    # ^TODO, once landed via macos support!
):
    child_tmp_file = tmp_path / "child.txt"
    child_tmp_file.touch()
    assert child_tmp_file.exists()
    path = str(child_tmp_file)

    # NOTE, this is expected to cancel the sub
    # in the `error_in_child=False` case!
    timeout: float = (
        1.6 if error_in_child
        else 1
    )
    try:
        with trio.move_on_after(timeout) as cs:
            async with tractor.open_nursery(
                loglevel=loglevel,
            ) as an:
                await (  # inlined `tractor.Portal`
                    await an.run_in_actor(
                        crash_and_clean_tmpdir,
                        tmp_file_path=path,
                        error=error_in_child,
                    )
                ).result()
    except (
        tractor.RemoteActorError,
        BaseExceptionGroup,
    ) as _exc:
        exc = _exc
        from tractor.log import get_console_log
        log = get_console_log(
            level=loglevel,
            name=__name__,
        )
        log.exception(
            f'Subactor failed as expected with {type(exc)!r}\n'
        )

    # tmp file should have been wiped by
    # teardown stack.
    assert not child_tmp_file.exists()

    if error_in_child:
        assert not cs.cancel_called
    else:
        # expect timeout in some cases?
        assert cs.cancel_called
