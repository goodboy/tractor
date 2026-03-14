'''
`tractor.devx.*` tooling sub-pkg test space.

'''
from __future__ import annotations
import platform
import signal
import time
from typing import (
    Callable,
    TYPE_CHECKING,
)

import pytest
from pexpect.exceptions import (
    TIMEOUT,
)
from pexpect.spawnbase import SpawnBase

from tractor._testing import (
    mk_cmd,
)
from tractor.devx.debug import (
    _pause_msg as _pause_msg,
    _crash_msg as _crash_msg,
    _repl_fail_msg as _repl_fail_msg,
    _ctlc_ignore_header as _ctlc_ignore_header,
)
from ..conftest import (
    _ci_env,
)

if TYPE_CHECKING:
    from pexpect import pty_spawn


_non_linux: bool = platform.system() != 'Linux'


def pytest_configure(config):
    # register custom marks to avoid warnings see,
    # https://docs.pytest.org/en/stable/how-to/writing_plugins.html#registering-custom-markers
    config.addinivalue_line(
        'markers',
        'ctlcs_bish: test will (likely) not behave under SIGINT..'
    )

# a fn that sub-instantiates a `pexpect.spawn()`
# and returns it.
type PexpectSpawner = Callable[
    [str],
    pty_spawn.spawn,
]


@pytest.fixture
def spawn(
    start_method: str,
    testdir: pytest.Pytester,
    reg_addr: tuple[str, int],

) -> PexpectSpawner:
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
        # disable colored tbs
        os.environ['PYTHON_COLORS'] = '0'
        # disable all ANSI color output
        # os.environ['NO_COLOR'] = '1'

    spawned: PexpectSpawner|None = None

    def _spawn(
        cmd: str,
        expect_timeout: float = 4,
        **mkcmd_kwargs,
    ) -> pty_spawn.spawn:
        nonlocal spawned
        unset_colors()
        spawned = testdir.spawn(
            cmd=mk_cmd(
                cmd,
                **mkcmd_kwargs,
            ),
            expect_timeout=(timeout:=(
                expect_timeout + 6
                if _non_linux and _ci_env
                else expect_timeout
            )),
            # preexec_fn=unset_colors,
            # ^TODO? get `pytest` core to expose underlying
            # `pexpect.spawn()` stuff?
        )
        # sanity
        assert spawned.timeout == timeout
        return spawned

    # such that test-dep can pass input script name.
    yield _spawn  # the `PexpectSpawner`, type alias.

    if (
        spawned
        and
        (ptyproc := spawned.ptyproc)
    ):
        start: float = time.time()
        timeout: float = 5
        while (
            ptyproc.isalive()
            and
            (
                (_time_took := (time.time() - start))
                 <
                 timeout
            )
        ):
            ptyproc.kill(signal.SIGINT)
            time.sleep(0.01)

        if ptyproc.isalive():
            ptyproc.kill(signal.SIGKILL)

    # TODO? ensure we've cleaned up any UDS-paths?
    # breakpoint()


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

        if (
            mark.name == 'ctlcs_bish'
            and
            use_ctlc
            and
            all(mark.args)
        ):
            pytest.skip(
                f'Test {node} prolly uses something from the stdlib (namely `asyncio`..)\n'
                f'The test and/or underlying example script can *sometimes* run fine '
                f'locally but more then likely until the cpython peeps get their sh#$ together, '
                f'this test will definitely not behave like `trio` under SIGINT..\n'
            )

    if use_ctlc:
        # XXX: disable pygments highlighting for auto-tests
        # since some envs (like actions CI) will struggle
        # the the added color-char encoding..
        from tractor.devx.debug import TractorConfig
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
    return str(child.before.decode())


def do_ctlc(
    child,
    count: int = 3,
    delay: float|None = None,
    patt: str|None = None,

    # expect repl UX to reprint the prompt after every
    # ctrl-c send.
    # XXX: no idea but, in CI this never seems to work even on 3.10 so
    # needs some further investigation potentially...
    expect_prompt: bool = not _ci_env,

) -> str|None:

    before: str|None = None
    delay = delay or 0.1

    # make sure ctl-c sends don't do anything but repeat output
    for _ in range(count):
        time.sleep(delay)
        child.sendcontrol('c')

        # TODO: figure out why this makes CI fail..
        # if you run this test manually it works just fine..
        if expect_prompt:
            time.sleep(delay)
            child.expect(
                PROMPT,
                timeout=(child.timeout * 2) if _ci_env else child.timeout,
            )
            before = str(child.before.decode())
            time.sleep(delay)

            if patt:
                # should see the last line on console
                assert patt in before

    # return the console content up to the final prompt
    return before
