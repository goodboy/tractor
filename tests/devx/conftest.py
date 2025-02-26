'''
`tractor.devx.*` tooling sub-pkg test space.

'''
import time
from typing import (
    Callable,
)

import pytest
from pexpect.exceptions import (
    TIMEOUT,
)
from pexpect.spawnbase import SpawnBase

from tractor._testing import (
    mk_cmd,
)
from tractor.devx._debug import (
    _pause_msg as _pause_msg,
    _crash_msg as _crash_msg,
    _repl_fail_msg as _repl_fail_msg,
    _ctlc_ignore_header as _ctlc_ignore_header,
)
from ..conftest import (
    _ci_env,
)


@pytest.fixture
def spawn(
    start_method,
    testdir: pytest.Pytester,
    reg_addr: tuple[str, int],

) -> Callable[[str], None]:
    '''
    Use the `pexpect` module shipped via `testdir.spawn()` to
    run an `./examples/..` script by name.

    '''
    if start_method != 'trio':
        pytest.skip(
            '`pexpect` based tests only supported on `trio` backend'
        )

    def unset_colors():
        '''
        Python 3.13 introduced colored tracebacks that break patt
        matching,

        https://docs.python.org/3/using/cmdline.html#envvar-PYTHON_COLORS
        https://docs.python.org/3/using/cmdline.html#using-on-controlling-color

        '''
        import os
        os.environ['PYTHON_COLORS'] = '0'

    def _spawn(
        cmd: str,
        **mkcmd_kwargs,
    ):
        unset_colors()
        return testdir.spawn(
            cmd=mk_cmd(
                cmd,
                **mkcmd_kwargs,
            ),
            expect_timeout=3,
            # preexec_fn=unset_colors,
            # ^TODO? get `pytest` core to expose underlying
            # `pexpect.spawn()` stuff?
        )

    # such that test-dep can pass input script name.
    return _spawn


@pytest.fixture(
    params=[False, True],
    ids='ctl-c={}'.format,
)
def ctlc(
    request,
    ci_env: bool,

) -> bool:

    use_ctlc = request.param

    node = request.node
    markers = node.own_markers
    for mark in markers:
        if mark.name == 'has_nested_actors':
            pytest.skip(
                f'Test {node} has nested actors and fails with Ctrl-C.\n'
                f'The test can sometimes run fine locally but until'
                ' we solve' 'this issue this CI test will be xfail:\n'
                'https://github.com/goodboy/tractor/issues/320'
            )

    if use_ctlc:
        # XXX: disable pygments highlighting for auto-tests
        # since some envs (like actions CI) will struggle
        # the the added color-char encoding..
        from tractor.devx._debug import TractorConfig
        TractorConfig.use_pygements = False

    yield use_ctlc


def expect(
    child,

    # normally a `pdb` prompt by default
    patt: str,

    **kwargs,

) -> None:
    '''
    Expect wrapper that prints last seen console
    data before failing.

    '''
    try:
        child.expect(
            patt,
            **kwargs,
        )
    except TIMEOUT:
        before = str(child.before.decode())
        print(before)
        raise


PROMPT = r"\(Pdb\+\)"


def in_prompt_msg(
    child: SpawnBase,
    parts: list[str],

    pause_on_false: bool = False,
    err_on_false: bool = False,
    print_prompt_on_false: bool = True,

) -> bool:
    '''
    Predicate check if (the prompt's) std-streams output has all
    `str`-parts in it.

    Can be used in test asserts for bulk matching expected
    log/REPL output for a given `pdb` interact point.

    '''
    __tracebackhide__: bool = False

    before: str = str(child.before.decode())
    for part in parts:
        if part not in before:
            if pause_on_false:
                import pdbp
                pdbp.set_trace()

            if print_prompt_on_false:
                print(before)

            if err_on_false:
                raise ValueError(
                    f'Could not find pattern in `before` output?\n'
                    f'part: {part!r}\n'
                )
            return False

    return True


# TODO: todo support terminal color-chars stripping so we can match
# against call stack frame output from the the 'll' command the like!
# -[ ] SO answer for stipping ANSI codes: https://stackoverflow.com/a/14693789
def assert_before(
    child: SpawnBase,
    patts: list[str],

    **kwargs,

) -> None:
    __tracebackhide__: bool = False

    assert in_prompt_msg(
        child=child,
        parts=patts,

        # since this is an "assert" helper ;)
        err_on_false=True,
        **kwargs
    )


def do_ctlc(
    child,
    count: int = 3,
    delay: float = 0.1,
    patt: str|None = None,

    # expect repl UX to reprint the prompt after every
    # ctrl-c send.
    # XXX: no idea but, in CI this never seems to work even on 3.10 so
    # needs some further investigation potentially...
    expect_prompt: bool = not _ci_env,

) -> str|None:

    before: str|None = None

    # make sure ctl-c sends don't do anything but repeat output
    for _ in range(count):
        time.sleep(delay)
        child.sendcontrol('c')

        # TODO: figure out why this makes CI fail..
        # if you run this test manually it works just fine..
        if expect_prompt:
            time.sleep(delay)
            child.expect(PROMPT)
            before = str(child.before.decode())
            time.sleep(delay)

            if patt:
                # should see the last line on console
                assert patt in before

    # return the console content up to the final prompt
    return before
