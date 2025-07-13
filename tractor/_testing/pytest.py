# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
`pytest` utils helpers and plugins for testing `tractor`'s runtime
and applications.

'''
from functools import (
    partial,
    wraps,
)
import inspect
import platform

import pytest
import tractor
import trio


def tractor_test(fn):
    '''
    Decorator for async test fns to decorator-wrap them as "native"
    looking sync funcs runnable by `pytest` and auto invoked with
    `trio.run()` (much like the `pytest-trio` plugin's approach).

    Further the test fn body will be invoked AFTER booting the actor
    runtime, i.e. from inside a `tractor.open_root_actor()` block AND
    with various runtime and tooling parameters implicitly passed as
    requested by by the test session's config; see immediately below.

    Basic deco use:
    ---------------

      @tractor_test
      async def test_whatever():
          await ...


    Runtime config via special fixtures:
    ------------------------------------
    If any of the following fixture are requested by the wrapped test
    fn (via normal func-args declaration),

    - `reg_addr` (a socket addr tuple where arbiter is listening)
    - `loglevel` (logging level passed to tractor internals)
    - `start_method` (subprocess spawning backend)

    (TODO support)
    - `tpt_proto` (IPC transport protocol key)

    they will be automatically injected to each test as normally
    expected as well as passed to the initial
    `tractor.open_root_actor()` funcargs.

    '''
    @wraps(fn)
    def wrapper(
        *args,
        loglevel=None,
        reg_addr=None,
        start_method: str|None = None,
        debug_mode: bool = False,
        **kwargs
    ):
        # __tracebackhide__ = True

        # NOTE: inject ant test func declared fixture
        # names by manually checking!
        if 'reg_addr' in inspect.signature(fn).parameters:
            # injects test suite fixture value to test as well
            # as `run()`
            kwargs['reg_addr'] = reg_addr

        if 'loglevel' in inspect.signature(fn).parameters:
            # allows test suites to define a 'loglevel' fixture
            # that activates the internal logging
            kwargs['loglevel'] = loglevel

        if start_method is None:
            if platform.system() == "Windows":
                start_method = 'trio'

        if 'start_method' in inspect.signature(fn).parameters:
            # set of subprocess spawning backends
            kwargs['start_method'] = start_method

        if 'debug_mode' in inspect.signature(fn).parameters:
            # set of subprocess spawning backends
            kwargs['debug_mode'] = debug_mode


        if kwargs:

            # use explicit root actor start
            async def _main():
                async with tractor.open_root_actor(
                    # **kwargs,
                    registry_addrs=[reg_addr] if reg_addr else None,
                    loglevel=loglevel,
                    start_method=start_method,

                    # TODO: only enable when pytest is passed --pdb
                    debug_mode=debug_mode,

                ):
                    await fn(*args, **kwargs)

            main = _main

        else:
            # use implicit root actor start
            main = partial(fn, *args, **kwargs)

        return trio.run(main)

    return wrapper


def pytest_addoption(
    parser: pytest.Parser,
):
    # parser.addoption(
    #     "--ll",
    #     action="store",
    #     dest='loglevel',
    #     default='ERROR', help="logging level to set when testing"
    # )

    parser.addoption(
        "--spawn-backend",
        action="store",
        dest='spawn_backend',
        default='trio',
        help="Processing spawning backend to use for test run",
    )

    parser.addoption(
        "--tpdb",
        "--debug-mode",
        action="store_true",
        dest='tractor_debug_mode',
        # default=False,
        help=(
            'Enable a flag that can be used by tests to to set the '
            '`debug_mode: bool` for engaging the internal '
            'multi-proc debugger sys.'
        ),
    )

    # provide which IPC transport protocols opting-in test suites
    # should accumulatively run against.
    parser.addoption(
        "--tpt-proto",
        nargs='+',  # accumulate-multiple-args
        action="store",
        dest='tpt_protos',
        default=['tcp'],
        help="Transport protocol to use under the `tractor.ipc.Channel`",
    )


def pytest_configure(config):
    backend = config.option.spawn_backend
    tractor._spawn.try_set_start_method(backend)


@pytest.fixture(scope='session')
def debug_mode(request) -> bool:
    '''
    Flag state for whether `--tpdb` (for `tractor`-py-debugger)
    was passed to the test run.

    Normally tests should pass this directly to `.open_root_actor()`
    to allow the user to opt into suite-wide crash handling.

    '''
    debug_mode: bool = request.config.option.tractor_debug_mode
    return debug_mode


@pytest.fixture(scope='session')
def spawn_backend(request) -> str:
    return request.config.option.spawn_backend


@pytest.fixture(scope='session')
def tpt_protos(request) -> list[str]:

    # allow quoting on CLI
    proto_keys: list[str] = [
        proto_key.replace('"', '').replace("'", "")
        for proto_key in request.config.option.tpt_protos
    ]

    # ?TODO, eventually support multiple protos per test-sesh?
    if len(proto_keys) > 1:
        pytest.fail(
            'We only support one `--tpt-proto <key>` atm!\n'
        )

    # XXX ensure we support the protocol by name via lookup!
    for proto_key in proto_keys:
        addr_type = tractor._addr._address_types[proto_key]
        assert addr_type.proto_key == proto_key

    yield proto_keys


@pytest.fixture(
    scope='session',
    autouse=True,
)
def tpt_proto(
    tpt_protos: list[str],
) -> str:
    proto_key: str = tpt_protos[0]

    from tractor import _state
    if _state._def_tpt_proto != proto_key:
        _state._def_tpt_proto = proto_key

    yield proto_key


@pytest.fixture(scope='session')
def reg_addr(
    tpt_proto: str,
) -> tuple[str, int|str]:
    '''
    Deliver a test-sesh unique registry address such
    that each run's (tests which use this fixture) will
    have no conflicts/cross-talk when running simultaneously
    nor will interfere with other live `tractor` apps active
    on the same network-host (namespace).

    '''
    from tractor._testing.addr import get_rando_addr
    return get_rando_addr(
        tpt_proto=tpt_proto,
    )


def pytest_generate_tests(
    metafunc: pytest.Metafunc,
):
    spawn_backend: str = metafunc.config.option.spawn_backend

    if not spawn_backend:
        # XXX some weird windows bug with `pytest`?
        spawn_backend = 'trio'

    # TODO: maybe just use the literal `._spawn.SpawnMethodKey`?
    assert spawn_backend in (
        'mp_spawn',
        'mp_forkserver',
        'trio',
    )

    # NOTE: used-to-be-used-to dyanmically parametrize tests for when
    # you just passed --spawn-backend=`mp` on the cli, but now we expect
    # that cli input to be manually specified, BUT, maybe we'll do
    # something like this again in the future?
    if 'start_method' in metafunc.fixturenames:
        metafunc.parametrize(
            "start_method",
            [spawn_backend],
            scope='module',
        )

    # TODO, parametrize any `tpt_proto: str` declaring tests!
    # proto_tpts: list[str] = metafunc.config.option.proto_tpts
    # if 'tpt_proto' in metafunc.fixturenames:
    #     metafunc.parametrize(
    #         'tpt_proto',
    #         proto_tpts,  # TODO, double check this list usage!
    #         scope='module',
    #     )
