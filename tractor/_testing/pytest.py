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
from typing import (
    Callable,
)

import pytest
import tractor
import trio


def tractor_test(
    wrapped: Callable|None = None,
    *,
    # @tractor_test(<deco-params>)
    timeout: float = 30,
    hide_tb: bool = True,
):
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

      @tractor_test(
        timeout=10,
      )
      async def test_whatever(
          # fixture param declarations
          loglevel: str,
          start_method: str,
          reg_addr: tuple,
          tpt_proto: str,
          debug_mode: bool,
      ):
          # already inside a root-actor runtime `trio.Task`
          await ...


    Runtime config via special fixtures:
    ------------------------------------
    If any of the following fixture are requested by the wrapped test
    fn (via normal func-args declaration),

    - `reg_addr` (a socket addr tuple where registrar is listening)
    - `loglevel` (logging level passed to tractor internals)
    - `start_method` (subprocess spawning backend)

    (TODO support)
    - `tpt_proto` (IPC transport protocol key)

    they will be automatically injected to each test as normally
    expected as well as passed to the initial
    `tractor.open_root_actor()` funcargs.

    '''
    __tracebackhide__: bool = hide_tb

    # handle @tractor_test (no parens) vs @tractor_test(timeout=10)
    if wrapped is None:
        return partial(
            tractor_test,
            timeout=timeout,
            hide_tb=hide_tb,
        )

    funcname: str = wrapped.__name__
    if not inspect.iscoroutinefunction(wrapped):
        raise TypeError(
            f'Test-fn {funcname!r} must be an async-function !!'
        )

    # NOTE: we intentionally use `functools.wraps` instead of
    # `@wrapt.decorator` here bc wrapt's transparent proxy makes
    # `inspect.iscoroutinefunction(wrapper)` return `True` (it
    # proxies `__code__` from the wrapped async fn), which causes
    # pytest to skip the test as an "unhandled coroutine".
    # `functools.wraps` preserves the signature for fixture
    # injection (via `__wrapped__`) without leaking the async
    # nature.
    @wraps(wrapped)
    def wrapper(**kwargs):
        __tracebackhide__: bool = hide_tb

        # NOTE, ensure we inject any test-fn declared fixture
        # names.
        for kw in [
            'reg_addr',
            'loglevel',
            'start_method',
            'debug_mode',
            'tpt_proto',
            'timeout',
        ]:
            if kw in inspect.signature(wrapped).parameters:
                assert kw in kwargs

        start_method = kwargs.get('start_method')
        if platform.system() == 'Windows':
            if start_method is None:
                kwargs['start_method'] = 'trio'
            elif start_method != 'trio':
                raise ValueError(
                    'ONLY the `start_method="trio"` is supported on Windows.'
                )

        # Open a root-actor, passing certain runtime-settings
        # extracted from the fixture kwargs, then invoke the
        # test-fn body as the root-most task.
        #
        # NOTE: we use `kwargs.get()` (not named params) so that
        # the fixture values remain in `kwargs` and are forwarded
        # to `wrapped()` — the test fn may declare the same
        # fixtures in its own signature.
        async def _main(**kwargs):
            __tracebackhide__: bool = hide_tb

            reg_addr = kwargs.get('reg_addr')
            with trio.fail_after(timeout):
                async with tractor.open_root_actor(
                    registry_addrs=(
                        [reg_addr] if reg_addr else None
                    ),
                    loglevel=kwargs.get('loglevel'),
                    start_method=kwargs.get('start_method'),

                    # TODO: only enable when pytest is passed
                    # --pdb
                    debug_mode=kwargs.get('debug_mode', False),

                ):
                    # invoke test-fn body IN THIS task
                    await wrapped(**kwargs)

        # invoke runtime via a root task.
        return trio.run(
            partial(
                _main,
                **kwargs,
            )
        )

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
    from tractor.spawn._spawn import try_set_start_method
    try_set_start_method(backend)

    # register custom marks to avoid warnings see,
    # https://docs.pytest.org/en/stable/how-to/writing_plugins.html#registering-custom-markers
    config.addinivalue_line(
        'markers',
        'no_tpt(proto_key): test will (likely) not behave with tpt backend'
    )


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
        from tractor.discovery import _addr
        addr_type = _addr._address_types[proto_key]
        assert addr_type.proto_key == proto_key

    yield proto_keys


@pytest.fixture(
    scope='session',
    autouse=True,
)
def tpt_proto(
    request,
    tpt_protos: list[str],
) -> str:
    proto_key: str = tpt_protos[0]

    # ?TODO, but needs a fn-scoped tpt_proto fixture..
    # @pytest.mark.no_tpt('uds')
    # node = request.node
    # markers = node.own_markers
    # for mark in markers:
    #     if (
    #         mark.name == 'no_tpt'
    #         and
    #         proto_key in mark.args
    #     ):
    #         pytest.skip(
    #             f'Test {node} normally fails with '
    #             f'tpt-proto={proto_key!r}\n'
    #         )

    from tractor.runtime import _state
    if _state._def_tpt_proto != proto_key:
        _state._def_tpt_proto = proto_key
        _state._runtime_vars['_enable_tpts'] = [
            proto_key,
        ]

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
