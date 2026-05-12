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
import os
import platform
from typing import (
    Callable,
    get_args,
    TYPE_CHECKING,
)
import warnings

import pytest
import tractor
from tractor.spawn._spawn import SpawnMethodKey
import trio

# Re-export `_testing.trace`'s pytest fixtures so they're
# picked up by pytest's plugin-discovery (this module is
# loaded via `pytest_plugins` from `pyproject.toml`). The
# `noqa: F401` annotations make linters tolerate the
# unused-looking imports — they're load-bearing for pytest
# discovery. The fixtures share their `name=` kw with the
# underlying CM functions; the python-level identifiers
# below carry the `_fixture` suffix to avoid module-scope
# collision (see `_testing/trace.py` for details).
from .trace import (  # noqa: F401
    afk_alarm_w_trace_fixture,
    fail_after_w_trace_fixture,
)

# Spawn-backend keys which may appear in `skipon_spawn_backend`
# marks ahead of the named backend actually being registered in
# `tractor.spawn._spawn.SpawnMethodKey`; such marks are inert
# (they can never match an active backend) but must not break
# collection.
_IN_DEV_SPAWN_BACKENDS: tuple[str, ...] = (
    'subint',
    'subint_forkserver',
    'main_thread_forkserver',
)

# Sub-plugin: zombie-subactor + UDS sock-file + shm
# reaping fixtures live in `tractor._testing._reap`
# alongside the underlying detection/cleanup helpers.
# Loading `_reap` as a sub-plugin here keeps reaping
# concerns co-located + this module focused on tractor-
# tooling-specific hooks (option/marker/parametrize,
# `tractor_test` deco, transport / spawn-method
# fixtures).
pytest_plugins: tuple[str, ...] = (
    'tractor._testing._reap',
)

if TYPE_CHECKING:
    from argparse import Namespace


_cap_sys_passed_as_flag: bool = False

# Spawn backends that need `--capture=sys` to avoid the
# fork-child×pytest-capture-fd deadlock. See the long
# NOTE in `pytest_load_initial_conftests` below for the
# full mechanism + tradeoff write-up.
_CAPSYS_REQUIRED_SPAWNERS: frozenset[str] = frozenset({
    'main_thread_forkserver',
    # TODO future variant-2 'subint_forkserver' lands
    # here too once the impl is unblocked.
})


# XXX REQUIRED in order to enforce `--capture=` flag
# pre test session.
# https://docs.pytest.org/en/stable/reference/reference.html#bootstrapping-hooks
@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config,
    parser: pytest.Parser,
    args: list[str],
):
    '''
    Validate the `--capture=` × `--spawn-backend=`
    combination at session-startup.

    Background
    ----------
    `--capture=sys` is REQUIRED for fork-based spawn backends (e.g.
    `main_thread_forkserver`): default `--capture=fd` redirects fd
    1,2 to temp files, and fork children inherit those fds — opaque
    deadlocks happen in the pytest-capture-machinery ↔ fork-child
    stdio interaction. `--capture=sys` only redirects Python- level
    `sys.stdout`/`sys.stderr`, leaving fd 1,2 alone.

    Trade-off (vs. `--capture=fd`):

    - LOST: per-test attribution of subactor *raw-fd* output (C-ext
      writes, `os.write(2, ...)`, subproc stdout). Not zero — those
      go to the terminal, captured by CI's terminal-level capture,
      just not per-test-scoped in the pytest failure report.

    - KEPT: Python-level `print()` + `logging` capture per-test
      (tractor's logger uses `sys.stderr`, so tractor log output IS
      still attributed per-test).

    - KEPT: user `pytest -s` for debugging (unaffected).

    Full post-mortem in
    `ai/conc-anal/subint_forkserver_test_cancellation_leak_issue.md`.

    Validation policy:
    - **CI mode** (`CI` env-var set): fail-fast at
      session start if a fork-spawn backend is requested
      WITHOUT `--capture=sys`. CI must be explicit; no
      auto-fallbacks. Forces every CI matrix-row's run
      line to declare its capture mode plainly.
    - **Local mode** (no `CI` env-var): emit a loud
      warning + suggest `--capture=sys`, but allow the
      run to proceed. Lets devs experiment with the bad
      combo (e.g. to validate whether recent
      fork-survival fixes have made `--capture=fd` work
      after all).

    '''
    global _cap_sys_passed_as_flag
    opts_w_args: Namespace = parser.parse_known_args(args)
    spawner: str|None = getattr(
        opts_w_args,
        'spawn_backend',
        None,
    )
    capture: str|None = getattr(
        opts_w_args,
        'capture',
        None,
    )
    if '--capture=sys' in args:
        _cap_sys_passed_as_flag = True
        assert capture == 'sys'

    in_ci: bool = bool(os.environ.get('CI'))

    if (
        spawner in _CAPSYS_REQUIRED_SPAWNERS
        and
        capture == 'fd'
    ):
        msg: str = (
            f'\n'
            f'XXX `--spawn-backend={spawner}` REQUIRES '
            f'`--capture=sys` XXX\n'
            f'fork-child × `--capture=fd` is a known '
            f'deadlock pattern.\n'
            f'See `tractor._testing.pytest`\'s '
            f'`pytest_load_initial_conftests` docstring '
            f'for the full mechanism.\n'
            f'\n'
            f'Re-invoke with `--capture=sys` (or run '
            f'with `pytest -s` for no capture).\n'
        )
        # fail-fast: CI must declare capture explicitly for
        # fork-spawn backends.
        if in_ci:
            pytest.exit(
                f'{msg}\n'
                f'FAIL-FAST: CI=1 detected; aborting session.\n',
                returncode=2,
            )

        # local: loud warn but let the run proceed so devs can
        # experiment.
        else:
            warnings.warn(
                f'{msg}\n'
                f'Local mode (no `CI` env var) — '
                f'continuing. Expect potential hangs.\n',
                category=UserWarning,
                stacklevel=1,
            )
            # ??TODO?? is there a way to force the `--capture=sys` sin CLI ??
            # - [x] ask pytest peeps in chat!
            # - [x] pytest` issue,
            #       https://github.com/pytest-dev/pytest/issues/14444

    # TODO, set various `$TRACTOR_X*` osenv vars here!
    print(
        f'Applying `tractor`-specific `pytest` config,\n'
        f'{opts_w_args!r}\n'
    )


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
    def wrapper(
        set_fork_aware_capture: pytest.CaptureFixture|None = None,
        # ^NOTE when set, the decorated fn declared as fixture-param.

        **kwargs,
    ):
        __tracebackhide__: bool = hide_tb

        # NOTE, ensure we inject any test-fn declared fixture
        # names.
        sig = inspect.signature(wrapped)
        for kw in [
            'reg_addr',
            'loglevel',
            'start_method',
            'debug_mode',
            'tpt_proto',
            'timeout',
        ]:
            if kw in sig.parameters:
                assert kw in kwargs

        if 'set_fork_aware_capture' in sig.parameters:
            assert set_fork_aware_capture
            kwargs['set_fork_aware_capture'] = set_fork_aware_capture

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
        dest='enable_stackscope',
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


def pytest_configure(
    config: pytest.Config,
):
    # opts: Namespace = config.option
    # print(
    #     f'PYTEST_CONFIGURE\n'
    #     f'capture={opts.capture!r}\n'
    # )
    # breakpoint()

    backend: str = config.option.spawn_backend
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
    config.addinivalue_line(
        'markers',
        'skipon_spawn_backend(*start_methods, reason=None): '
        'skip this test under any of the given `--spawn-backend` '
        'values; useful for backend-specific known-hang / -borked '
        'cases (e.g. the `subint` GIL-starvation class documented '
        'in `ai/conc-anal/subint_sigint_starvation_issue.md`).'
    )

    # `--enable-stackscope`: install SIGUSR1 → trio task-tree
    # dump in pytest itself + propagate to every subactor via
    # an env var that fork-children inherit and the runtime
    # gate honors. Lighter than `--tpdb` (no pdb machinery) —
    # purely for hang-investigation stack visibility.
    if getattr(
        config.option,
        'enable_stackscope',
        False
    ):
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
            warnings.warn(
                '`stackscope` not installed — '
                '--enable-stackscope is a no-op. '
                'Install via the `devx` dep group.'
            )
    else:
        os.environ.pop('TRACTOR_ENABLE_STACKSCOPE', None)


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Function],
):
    '''
    Expand any `@pytest.mark.skipon_spawn_backend('<backend>'[,
    ...], reason='...')` markers into concrete
    `pytest.mark.skip(reason=...)` calls for tests whose
    backend-arg set contains the active `--spawn-backend`.

    Uses `item.iter_markers(name=...)` which walks function +
    class + module-level marks in the correct scope order (and
    handles both the single-`MarkDecorator` and `list[Mark]`
    forms of a module-level `pytestmark`) — so the same marker
    works at any level a user puts it.

    '''
    backend: str = config.option.spawn_backend
    default_reason: str = f'Borked on --spawn-backend={backend!r}'
    for item in items:
        for mark in item.iter_markers(name='skipon_spawn_backend'):
            skip_backends: tuple[str] = mark.args
            for skip_backend in skip_backends:
                assert (
                    skip_backend in get_args(SpawnMethodKey)
                    or
                    skip_backend in _IN_DEV_SPAWN_BACKENDS
                )
            # ?TODO, run these through the try-set-backend checker to
            # avoid typos?
            if backend in skip_backends:
                reason: str = mark.kwargs.get(
                    'reason',
                    default_reason,
                )
                item.add_marker(pytest.mark.skip(reason=reason))
                # first matching mark wins; no value in stacking
                # multiple `skip`s on the same item.
                break


@pytest.fixture(
    scope="session",
    autouse=True,
)
def alert_on_finish():
    '''
    Ring a terminal notification on full test session
    completion to alert any would be human.

    '''
    # TODO, check attached to tty or skip!
    yield  # run all tests
    print("\a")  # trigger terminal bell
    # ?TODO, any other nice-tricks/specific tuis we could try?
    # - supposedly works in many terminals:
    #   >> print("\033]5;Alert: Tests Finished\a")
    # - sway/i3-nag?


@pytest.fixture(scope='session')
def debug_mode(
    request: pytest.FixtureRequest,
) -> bool:
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
def tpt_protos(
    request: pytest.FixtureRequest,
) -> list[str]:

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
    request: pytest.FixtureRequest,
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
    assert spawn_backend in get_args(SpawnMethodKey)

    # NOTE: used-to-be-used-to dyanmically parametrize tests for when
    # you just passed --spawn-backend=`mp` on the cli, but now we expect
    # that cli input to be manually specified, BUT, maybe we'll do
    # something like this again in the future?
    if 'start_method' in metafunc.fixturenames:
        metafunc.parametrize(
            "start_method",
            [spawn_backend],
            scope='session',
            ids=lambda item: f'start_method={spawn_backend}',
        )

    # TODO, parametrize any `tpt_proto: str` declaring tests!
    # proto_tpts: list[str] = metafunc.config.option.proto_tpts
    # if 'tpt_proto' in metafunc.fixturenames:
    #     metafunc.parametrize(
    #         'tpt_proto',
    #         proto_tpts,  # TODO, double check this list usage!
    #         scope='module',
    #     )

def _is_forking_spawner(
    start_method: str,
) -> bool:
    return start_method in [
        'main_thread_forkserver',
        'mp_forkserver',
    ]


@pytest.fixture(scope='session')
def is_forking_spawner(
    start_method: str,
) -> bool:
    '''
    Is the `pytest` run using a `fork()`ing process spawning-backend?

    '''
    return _is_forking_spawner


def maybe_xfail_for_spawner(
    request: pytest.FixtureRequest,
    start_method: str,
    is_forking_spawner: bool,
) -> None:
    '''
    Fork based spawning backends cause issues with
    `pytest`'s fd-capture mechanism and can cause various
    suites to hang.

    This helper allows skipping/xfailing from a test when
    a fork-spawn backend is being used WITHOUT
    `--capture=sys`.

    '''
    capture_mode: str = request.config.option.capture
    # `tee-sys` is also sys-level capture (just additionally writes
    # to the original `sys.__stdout__/__stderr__`); fork-safe like
    # `sys`. Only `fd`-level capture is the deadlock pattern.
    if (
        capture_mode not in (
            'sys',
            'tee-sys',
        )
        and
        is_forking_spawner
    ):
        pytest.skip(
            f'Spawner {start_method!r} requires the flag,\n'
            f'--capture=sys or --capture=tee-sys..\n'
            f'(got --capture={capture_mode!r})\n'
        )


def maybe_override_capture(
    request: pytest.FixtureRequest,
    start_method: bool,
) -> str:
    if _is_forking_spawner(start_method):
        request.getfixturevalue('capsys')
        return 'sys'

    return request.config.option.capture


@pytest.fixture
def set_fork_aware_capture(
    request: pytest.FixtureRequest,
    start_method: str,
) -> pytest.CaptureFixture|str:
    '''
    Force `--capture=sys` method for tests using
    a forking-spawner backend due to fd-copying issues
    which can oddly make certain tests hang/fail.

    '''
    # Fast-path: user already passed sys-level capture
    # (`sys` or `tee-sys`) at the CLI — no override needed.
    if request.config.option.capture in (
            'sys',
            'tee-sys',
    ):
        return request.config.option.capture

    capsys: pytest.CaptureFixture = maybe_override_capture(
        request=request,
        start_method=start_method,
    )
    return capsys
    # XXX reset?
    # with capsys.disabled():
    #     pass
    # return partial(
    #     maybe_override_capture,
    #     request=request,
    #     start_method=start_method,
    # )


def pytest_terminal_summary(
    terminalreporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    '''
    End-of-session summary: list all
    `fail_after_w_trace`/`afk_alarm_w_trace` snapshot dirs
    captured during the run so the human doesn't have to scroll
    back through captured-stderr lines to find dump paths.

    Reads from `tractor._testing.trace._SNAPSHOT_INDEX` which is
    populated by `_do_capture_snapshot()` on each successful
    snapshot capture.

    No-op when zero snapshots were captured (most sessions).

    '''
    from .trace import _SNAPSHOT_INDEX

    if not _SNAPSHOT_INDEX:
        return

    tr = terminalreporter
    tr.write_sep('=', 'tractor hang-snapshot index')
    tr.write_line(
        f'{len(_SNAPSHOT_INDEX)} `fail_after_w_trace` / '
        f'`afk_alarm_w_trace` snapshot(s) captured this session:'
    )
    for label, path in _SNAPSHOT_INDEX:
        tr.write_line(f'  {label}')
        tr.write_line(f'    → {path}')
