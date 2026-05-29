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
    get_args,
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

        # Extract runtime settings as locals for
        # `open_root_actor()`; these must NOT leak into
        # `kwargs` when the test fn doesn't declare them
        # (the original pre-wrapt code had the same guard).
        reg_addr = kwargs.get('reg_addr')
        loglevel = kwargs.get('loglevel')
        debug_mode = kwargs.get('debug_mode', False)
        start_method = kwargs.get('start_method')
        if platform.system() == 'Windows':
            if start_method is None:
                start_method = 'trio'
            elif start_method != 'trio':
                raise ValueError(
                    'ONLY the `start_method="trio"` is supported on Windows.'
                )

        # Open a root-actor, passing runtime-settings
        # extracted above as closure locals, then invoke
        # the test-fn body as the root-most task.
        #
        # NOTE: `kwargs` is forwarded as-is to
        # `wrapped()` — it only contains what pytest
        # injected based on the test fn's signature.
        async def _main(**kwargs):
            __tracebackhide__: bool = hide_tb

            with trio.fail_after(timeout):
                async with tractor.open_root_actor(
                    registry_addrs=(
                        [reg_addr] if reg_addr else None
                    ),
                    loglevel=loglevel,
                    start_method=start_method,

                    # TODO: only enable when pytest is passed
                    # --pdb
                    debug_mode=debug_mode,

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

    parser.addoption(
        "--enable-stackscope",
        action="store_true",
        dest='tractor_enable_stackscope',
        default=False,
        help=(
            'Install `stackscope` SIGUSR1 handler in pytest + '
            'every spawned subactor for live trio task-tree '
            'dumps during hang investigations. Lighter than '
            '`--tpdb` (no pdb machinery / tty-lock contention) '
            '— use when you only need stack visibility. To '
            'capture: `kill -USR1 <pytest-or-subactor-pid>`.'
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

    # console loglevel for the test-session, scoped to the
    # consuming-project's OWN pkg-hierarchy (see the
    # `testing_pkg_name` fixture). For `tractor` itself this IS the
    # runtime loglevel; downstream projects use `--ll` for their own
    # ("internal") app-logging and `--tl` for tractor-as-runtime.
    parser.addoption(
        "--ll",
        "--loglevel",
        action="store",
        dest='loglevel',
        default=None,
        help=(
            "console loglevel to set for the test session, scoped to "
            "the consuming-project pkg (see `testing_pkg_name`). "
            "Falls through as the `--tl` default."
        ),
    )

    # tractor-as-runtime loglevel, DISTINCT from `--ll` so downstream
    # projects can split their app-logs from the `tractor.*` runtime
    # hierarchy. Accepts a `tractor.log` "logging-spec" (see
    # `tractor.log.apply_logspec()`).
    parser.addoption(
        "--tl",
        "--tractor-loglevel",
        action="store",
        dest='tractor_loglevel',
        default=None,
        help=(
            "loglevel (or logging-spec) for `tractor`-as-runtime, "
            "distinct from `--ll`. Accepts a bare level (eg. "
            "'info', 'cancel') or a sub-logger filter-spec, "
            "'<sublog>:<level>,...' (eg. "
            "'devx:runtime,trionics:cancel'). Falls back to `--ll` "
            "when unset. Mirrors the logging-spec grammar consumed "
            "by `tractor.log.apply_logspec()` (see its sub-pkg "
            "granularity caveat)."
        ),
    )


def pytest_configure(config):
    backend = config.option.spawn_backend
    from tractor.spawn._spawn import try_set_start_method
    try:
        try_set_start_method(backend)
    except RuntimeError as err:
        # e.g. `--spawn-backend=subint` on Python < 3.14 — turn the
        # runtime gate error into a clean pytest usage error so the
        # suite exits with a helpful banner instead of a traceback.
        raise pytest.UsageError(str(err)) from err

    # register custom marks to avoid warnings see,
    # https://docs.pytest.org/en/stable/how-to/writing_plugins.html#registering-custom-markers
    config.addinivalue_line(
        'markers',
        'no_tpt(proto_key): test will (likely) not behave with tpt backend'
    )

    # `--enable-stackscope`: install SIGUSR1 → trio task-tree
    # dump in pytest itself + propagate to every subactor via
    # an env var that fork-children inherit and the runtime
    # gate honors. Lighter than `--tpdb` (no pdb machinery) —
    # purely for hang-investigation stack visibility.
    if getattr(
        config.option, 'tractor_enable_stackscope', False
    ):
        import os
        # Env var inherited via fork → subactor's runtime
        # picks it up at `Actor.async_main` startup. See the
        # gate in `tractor.runtime._runtime` matching this
        # var name.
        os.environ['TRACTOR_ENABLE_STACKSCOPE'] = '1'

        # Install in pytest itself so `kill -USR1 <pytest>`
        # dumps the parent trio task-tree (which is where
        # most Mode-A-class hangs park).
        try:
            from tractor.devx._stackscope import (
                enable_stack_on_sig,
            )
            enable_stack_on_sig()
        except ImportError:
            import warnings
            warnings.warn(
                '`stackscope` not installed — '
                '--enable-stackscope is a no-op. '
                'Install via the `devx` dep group.'
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
def testing_pkg_name() -> str:
    '''
    Root pkg-name of the project consuming this plugin, used to
    scope `--ll` "internal"/app-level console logging into that
    project's OWN `tractor.log.get_logger(pkg_name=<.>)` hierarchy
    — distinct from the `tractor.*` runtime hierarchy configured
    via `--tl`.

    Defaults to `'tractor'` (so tractor's own suite treats `--ll`
    as the runtime level). Downstream projects override this from
    their `conftest.py`, eg.

    .. code:: python

        @pytest.fixture(scope='session')
        def testing_pkg_name() -> str:
            return 'modden'

    '''
    return 'tractor'


@pytest.fixture(
    scope='session',
    autouse=True,
)
def loglevel(
    request: pytest.FixtureRequest,
    testing_pkg_name: str,
) -> str|None:
    '''
    Resolve + apply the test-session console loglevels and yield
    the `tractor`-runtime level (also passed to
    `open_root_actor(loglevel=<.>)` by `@tractor_test`).

    - `--tl <logspec>`: tractor-runtime level (falls back to the
      generic `--ll`); applied to the `tractor.*` logger hierarchy
      and `tractor.log._default_loglevel` via
      `tractor.log.apply_logspec()`.
    - `--ll <level>`: the consuming-project's OWN console loglevel,
      applied to its `testing_pkg_name` hierarchy when that isn't
      `tractor` itself.

    '''
    import tractor
    orig: str = tractor.log._default_loglevel

    ll: str|None = request.config.option.loglevel
    tl: str|None = request.config.option.tractor_loglevel

    # tractor-runtime loglevel: explicit `--tl` wins, else fall
    # back to the generic `--ll`, else leave the lib default.
    logspec: str|None = tl if tl is not None else ll
    tractor_level: str|None = None
    if logspec is not None:
        tractor_level, _ = tractor.log.apply_logspec(
            logspec,
            default_level=ll,
            pkg_name='tractor',
        )
        if tractor_level is not None:
            tractor.log._default_loglevel = tractor_level

    # consuming-project ("internal") console logging at the generic
    # `--ll` level, scoped to ITS OWN pkg-hierarchy (NOT `tractor.*`)
    # so downstream projects can split app-logs from runtime-logs.
    if (
        ll is not None
        and
        testing_pkg_name
        and
        testing_pkg_name != 'tractor'
    ):
        tractor.log.get_console_log(
            level=ll,
            pkg_name=testing_pkg_name,
            name=testing_pkg_name,
        )

    log = tractor.log.get_console_log(
        level=tractor_level,
        name='tractor',  # <- enable root logger
    )
    log.info(
        f'Test-harness set session loglevels:\n'
        f'tractor-runtime (`--tl`/`--ll`): {tractor_level!r}\n'
        f'{testing_pkg_name!r} (`--ll`): {ll!r}\n'
    )
    yield tractor_level
    tractor.log._default_loglevel = orig


@pytest.fixture(scope='function')
def test_log(
    request: pytest.FixtureRequest,
    loglevel: str,
    testing_pkg_name: str,
) -> tractor.log.StackLevelAdapter:
    '''
    Deliver a per test-module-fn logger instance for reporting from
    within actual test bodies/fixtures.

    For example this can be handy to report certain error cases from
    exception handlers using `test_log.exception()`.

    The logger is scoped to the consuming-project's
    `testing_pkg_name` hierarchy so downstream suites' in-test logs
    land under their own pkg, not `tractor.*`.

    '''
    modname: str = request.function.__module__
    log = tractor.log.get_logger(
        name=modname,
        pkg_name=testing_pkg_name,
    )
    _log = tractor.log.get_console_log(
        level=loglevel,
        logger=log,
        name=modname,
    )
    _log.debug(
        f'In-test-logging requested\n'
        f'test_log.name: {log.name!r}\n'
        f'level: {loglevel!r}\n'
    )
    yield _log


@pytest.fixture(scope='session')
def spawn_backend(
    request: pytest.FixtureRequest,
) -> str:
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

    # drive the valid-backend set from the canonical `Literal` so
    # adding a new spawn backend (e.g. `'subint'`) doesn't require
    # touching the harness.
    from tractor.spawn._spawn import SpawnMethodKey
    assert spawn_backend in get_args(SpawnMethodKey)

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
