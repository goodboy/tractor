'''
`tractor.log`-wrapping unit tests.

'''
from collections.abc import Callable, Iterator
import importlib
from functools import partial
import logging
from pathlib import Path
import shutil
import sys
from types import ModuleType

import pytest
import tractor
import trio
from tractor import (
    _code_load,
    log,
)
from tractor.ipc import _chan


def test_root_pkg_not_duplicated_in_logger_name():
    '''
    When both `pkg_name` and `name` are passed and they have
    a common `<root_name>.< >` prefix, ensure that it is not
    duplicated in the child's `StackLevelAdapter.name: str`.

    Also pins the explicit-`name` contract: an explicitly passed
    dotted `name` is treated as a *literal* sub-logger path and is
    NOT leaf-collapsed. The leaf-module is only dropped when the
    trailing token duplicates the *caller's own* `__name__` leaf (the
    `{filename}` field) — see `test_implicit_mod_name_applied_for_child`
    for that (auto-naming) path. This is what keeps a real (possibly
    nested) sub-PACKAGE like `subpkg.mod` -> `devx.debug` addressable
    by the `tractor.log` logging-spec, instead of collapsing to its
    parent.

    '''
    project_name: str = 'pylib'
    pkg_path: str = 'pylib.subpkg.mod'

    assert not tractor.current_actor(
        err_on_no_runtime=False,
    )
    proj_log = log.get_logger(
        pkg_name=project_name,
        mk_sublog=False,
    )

    sublog = log.get_logger(
        pkg_name=project_name,
        name=pkg_path,
    )

    assert proj_log is not sublog
    # the root pkg-name appears exactly once (no `pylib.pylib...`)
    assert sublog.name.count(proj_log.name) == 1
    # explicit dotted `name` is preserved literally (NOT collapsed);
    # the trailing token survives since it's not the *caller's* own
    # leaf-module (`test_log_sys`), so this is treated as a literal
    # sub-pkg path.
    assert sublog.name == f'{project_name}.subpkg.mod'


def test_implicit_mod_name_applied_for_child(
    testdir: pytest.Pytester,
    loglevel: str,
):
    '''
    Verify that when `.log.get_logger(pkg_name='pylib')` is called
    from a given sub-mod from within the `pylib` pkg-path, we
    implicitly set the equiv of `name=__name__` from the caller's
    module.

    '''
    # tractor.log.get_console_log(level=loglevel)
    proj_name: str = 'snakelib'
    mod_code: str = (
        f'import tractor\n'
        f'\n'
        # if you need to trace `testdir` stuff @ import-time..
        # f'breakpoint()\n'
        f'log = tractor.log.get_logger(pkg_name="{proj_name}")\n'
    )

    # create a sub-module for each pkg layer
    _lib = testdir.mkpydir(proj_name)
    pkg: Path = Path(_lib)
    pkg_init_mod: Path = pkg / "__init__.py"
    pkg_init_mod.write_text(mod_code)

    subpkg: Path = pkg / 'subpkg'
    subpkg.mkdir()
    subpkgmod: Path = subpkg / "__init__.py"
    subpkgmod.touch()
    subpkgmod.write_text(mod_code)

    _submod: Path = testdir.makepyfile(
        _mod=mod_code,
    )

    pkg_submod = pkg / 'mod.py'
    pkg_subpkg_submod = subpkg / 'submod.py'
    shutil.copyfile(
        _submod,
        pkg_submod,
    )
    shutil.copyfile(
        _submod,
        pkg_subpkg_submod,
    )
    testdir.chdir()
    # NOTE, to introspect the py-file-module-layout use (in .xsh
    # syntax): `ranger @str(testdir)`

    # XXX NOTE, once the "top level" pkg mod has been
    # imported, we can then use `import` syntax to
    # import it's sub-pkgs and modules.
    subpkgmod: ModuleType = _code_load.load_module_from_path(
        Path(pkg / '__init__.py'),
        module_name=proj_name,
    )

    pkg_root_log = log.get_logger(
        pkg_name=proj_name,
        mk_sublog=False,
    )
    # the top level pkg-mod, created just now,
    # by above API call.
    assert pkg_root_log.name == proj_name
    assert not pkg_root_log.logger.getChildren()
    #
    # ^TODO! test this same output but created via a `get_logger()`
    # call in the `snakelib.__init__py`!!

    # NOTE, the pkg-level "init mod" should of course
    # have the same name as the package ns-path.
    import snakelib as init_mod
    assert init_mod.log.name == proj_name

    # NOTE, a first-pkg-level sub-module should only
    # use the package-name since the leaf-node-module
    # will be included in log headers by default.
    from snakelib import mod
    assert mod.log.name == proj_name

    from snakelib import subpkg
    assert (
        subpkg.log.name
        ==
        subpkg.__package__ 
        ==
        f'{proj_name}.subpkg'
    )

    from snakelib.subpkg import submod
    assert (
        submod.log.name
        ==
        submod.__package__ 
        ==
        f'{proj_name}.subpkg'
    )

    sub_logs = pkg_root_log.logger.getChildren()
    assert len(sub_logs) == 1  # only one nested sub-pkg module
    assert submod.log.logger in sub_logs


def test_implicit_mod_name_from_unregistered_namespace(
    tmp_path: Path,
):
    '''
    Preserve implicit logger naming for dynamic module namespaces.

    The fast `sys.modules` caller lookup cannot resolve `runpy`,
    plugin-loader, or `exec()` namespaces that are not registered.
    Compile a real package file under an unregistered module name so
    the rare filename fallback must recover its imported package and
    retain the same package-level logger name.

    '''
    pkg_name = 'dynamic_logger_pkg'
    pkg_dir = tmp_path / pkg_name
    pkg_dir.mkdir()
    init_path = pkg_dir / '__init__.py'
    init_path.write_text('')
    mod_path = pkg_dir / 'plugin.py'
    mod_path.write_text('')

    sys.path.insert(0, str(tmp_path))
    try:
        importlib.import_module(pkg_name)
        namespace = {
            '__name__': f'{pkg_name}.unregistered',
            '__package__': pkg_name,
            'tractor': tractor,
        }
        exec(
            compile(
                'log = tractor.log.get_logger('
                f'pkg_name={pkg_name!r})',
                str(mod_path),
                'exec',
            ),
            namespace,
        )
        dynamic_log = namespace.get('log')
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop(pkg_name, None)

    assert dynamic_log is not None
    assert dynamic_log.name == pkg_name


def test_io_custom_level_registered():
    '''
    The `IO`(21) level (registered via `add_log_level()` at
    import, for `tractor.trionics._subproc`'s std-stream relay)
    is fully wired and SHOWN BY DEFAULT at `info`-level consoles
    since `21 >= INFO(20)`.

    '''
    import logging
    assert log.CUSTOM_LEVELS.get('IO') == 21
    assert logging.getLevelName(21) == 'IO'
    assert log.STD_PALETTE.get('IO')
    assert log.BOLD_PALETTE['bold'].get('IO')

    iolog = log.get_logger('io_lvl_test')
    assert callable(getattr(iolog, 'io', None))
    # emit must not raise
    iolog.io('hello from the IO level')

    # 21 >= INFO(20) -> shown when console set to `info`
    assert 21 >= logging.INFO


def test_add_log_level_pluggable():
    '''
    `add_log_level()` is the single pluggable entry-point: one
    call wires `CUSTOM_LEVELS` + `addLevelName` + both palettes +
    a same-named `StackLevelAdapter` emit method (so
    `get_logger()`'s per-level audit passes).

    '''
    import logging
    name: str = 'XLVL'
    val: int = 19
    try:
        log.add_log_level(name, val, 'cyan')

        assert log.CUSTOM_LEVELS[name] == val
        assert logging.getLevelName(val) == name
        assert log.STD_PALETTE[name] == 'cyan'
        assert log.BOLD_PALETTE['bold'][name] == 'bold_cyan'

        # the audit in `get_logger()` (asserts a method per
        # `CUSTOM_LEVELS` entry) must still pass.
        xlog = log.get_logger('xlvl_test')
        emit = getattr(xlog, name.lower(), None)
        assert callable(emit)
        emit('hello from a plugged-in level')

    finally:
        # best-effort cleanup of our module-global mutations so
        # later `get_logger()` audits don't see a half-removed
        # level.
        log.CUSTOM_LEVELS.pop(name, None)
        log.STD_PALETTE.pop(name, None)
        log.BOLD_PALETTE['bold'].pop(name, None)
        if hasattr(log.StackLevelAdapter, name.lower()):
            delattr(log.StackLevelAdapter, name.lower())


@pytest.fixture(params=[False, True], ids=['raw', 'adapter'])
def level_guard(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[logging.Logger, Callable[[int|str], bool]]]:
    '''
    Return an INFO(20) logger and a log-level enablement query.

    `is_info_level_enabled(level)` returns True when the requested
    log level meets the logger's current threshold and logging is
    not disabled. It queries that decision; it does not emit a msg.
    The initial INFO threshold permits INFO(20), CANCEL(22) and
    WARNING(30), but blocks DEBUG(10) and NOTSET(0). Tests may change
    the logger's threshold or disable flag to query those settings.

    Each case allocates a fresh raw `logging.Logger` as `std_log`;
    consuming tests do not supply a logger. `request.param` selects
    only the returned query:
    - False (`raw`): bind `tractor.log.at_least_level` to `std_log`.
    - True (`adapter`): wrap that same logger in `StackLevelAdapter`
      and return its `at_least_level` method. The raw logger never
      supplies an `at_least_level` method.

    Register only `std_log` under its name in
    `Logger.manager.loggerDict` so `Logger.setLevel()` and
    `logging.disable()` can invalidate its enabled-level cache.
    `std_log.setLevel(...)` and changes to `std_log.disabled` affect
    that logger, not other loggers. The registration is temporary;
    `logging.disable()` changes the process-global cutoff. Restore
    that cutoff and the registration after each case.

    A consumer can use either query form in the same way::

        def test_info_query(level_guard):
            std_log, is_info_level_enabled = level_guard
            assert std_log.level == logging.INFO
            assert is_info_level_enabled('INFO')
            assert not is_info_level_enabled('DEBUG')

    '''
    std_log = logging.Logger('level_guard_test', logging.INFO)
    monkeypatch.setitem(
        logging.Logger.manager.loggerDict,
        std_log.name,
        std_log,
    )
    orig_disable: int = logging.root.manager.disable
    # Clear inherited global suppression so INFO queries can pass.
    # NOTSET(0) removes logging.disable()'s positive-level cutoff.
    logging.disable(logging.NOTSET)
    try:
        is_info_level_enabled: Callable[[int|str], bool]
        if request.param:
            guarded_log = log.StackLevelAdapter(std_log, {})
            is_info_level_enabled = guarded_log.at_least_level
        else:
            is_info_level_enabled = partial(
                log.at_least_level, std_log,
            )

        yield std_log, is_info_level_enabled
    finally:
        logging.disable(orig_disable)


@pytest.mark.parametrize(
    'level, enabled',
    [
        # String log levels are compared with the INFO(20) threshold.
        ('INFO', True),
        ('info', True),
        ('WARNING', True),
        ('warning', True),
        ('DEBUG', False),
        ('debug', False),
        ('ERROR', True),
        ('CRITICAL', True),
        ('WARN', True),  # WARNING(30) alias, above INFO(20).
        ('FATAL', True),  # CRITICAL(50) alias, above INFO(20).
        ('CANCEL', True),  # Custom CANCEL(22), above INFO(20).
        ('cancel', True),
        ('TRANSPORT', False),  # TRANSPORT(5), below INFO(20).
        (logging.INFO, True),  # 20 equals the INFO(20) threshold.
        (logging.DEBUG, False),  # 10 is below INFO(20).
        (22, True),  # CANCEL(22) is above INFO(20).
        (0, False),  # NOTSET(0) is below INFO(20).
        ('NOTSET', False),
        ('notset', False),
    ],
)
def test_at_least_level_names_and_integers(
    level_guard,
    level: int|str,
    enabled: bool,
):
    '''
    String names of built-in log levels must not raise `KeyError`.

    `tractor.log.at_least_level()` indexed `CUSTOM_LEVELS` for every
    string query, so built-in logging levels such as 'INFO' raised
    `KeyError` before testing whether logging was enabled. Query the
    documented log levels and aliases, lowercase spellings, custom
    levels and integer equivalents. Each result must match its
    expected INFO(20) threshold comparison without changing that
    threshold; NOTSET must resolve even though its value is zero.

    See Python's `logging` docs for built-in level values:
    https://docs.python.org/3/library/logging.html#logging-levels

    '''
    std_log, is_info_level_enabled = level_guard
    assert is_info_level_enabled(level) is enabled
    # A query must not reconfigure the INFO(20) logger.
    assert std_log.level == logging.INFO


def test_at_least_level_registered_name(
    level_guard,
    monkeypatch: pytest.MonkeyPatch,
):
    '''
    Query a custom log level registered with `logging.addLevelName()`.

    `tractor.log.at_least_level()` raised `KeyError` when a string
    log level was absent from `CUSTOM_LEVELS`, even when registered
    by `logging.addLevelName()`. Register APP_LEVEL_GUARD(35), between
    WARNING(30) and ERROR(40). Its lowercase query must pass at
    INFO(20), and its uppercase query must fail at ERROR(40). Restore
    the logging registrations afterward so later tests do not inherit
    this custom level.

    '''
    monkeypatch.setattr(
        logging, '_nameToLevel', logging._nameToLevel.copy(),
    )
    monkeypatch.setattr(
        logging, '_levelToName', logging._levelToName.copy(),
    )
    # Custom 35 is above INFO(20), but below ERROR(40).
    logging.addLevelName(35, 'APP_LEVEL_GUARD')
    std_log, is_level_enabled = level_guard
    assert is_level_enabled('app_level_guard') is True
    std_log.setLevel(logging.ERROR)
    assert is_level_enabled('APP_LEVEL_GUARD') is False


@pytest.mark.parametrize('level', ['unknown_level_guard', '20', ''])
def test_at_least_level_unknown_name(
    level_guard,
    level: str,
):
    '''
    Reject unknown log-level names with an explicit `KeyError`.

    The old `CUSTOM_LEVELS` lookup raised `KeyError` with the
    uppercase query as its key. Log-level name lookup must not
    silently enable misspelled levels or treat numeric strings as
    integer inputs. Each invalid name must still raise `KeyError`
    carrying its uppercase spelling, including an empty name.

    '''
    std_log, is_info_level_enabled = level_guard
    with pytest.raises(KeyError) as excinfo:
        is_info_level_enabled(level)
    assert excinfo.value.args == (level.upper(),)
    assert std_log.level == logging.INFO


@pytest.mark.parametrize('level', ['info', 'cancel', logging.INFO, 22])
@pytest.mark.parametrize('suppression', ['logger', 'global'])
def test_at_least_level_disable_controls(
    level_guard,
    level: int|str,
    suppression: str,
):
    '''
    Log-level name resolution must honor logging disable controls.

    Earlier guards checked only `Logger.getEffectiveLevel()` and
    rendered payloads despite disable controls. Prime a positive
    enabled-level result, then disable the logger or all logging.
    Built-in INFO and custom CANCEL string names, plus their integer
    equivalents, must return False even though the logger's effective
    INFO(20) threshold remains unchanged. Query once before disabling
    to ensure a cached True result cannot bypass suppression.

    '''
    std_log, is_info_level_enabled = level_guard
    assert is_info_level_enabled(level) is True
    if suppression == 'logger':
        std_log.disabled = True
    else:
        logging.disable(logging.CRITICAL)

    assert is_info_level_enabled(level) is False
    assert std_log.getEffectiveLevel() == logging.INFO


@pytest.mark.parametrize(
    'suppression',
    [
        'level',
        'logger',
        'global',
    ],
)
def test_log_guard_skips_payload_formatting(
    monkeypatch: pytest.MonkeyPatch,
    suppression: str,
):
    '''
    Suppressed transport logs must not render payloads.

    The original hot-path guard compared only the effective logger
    level. A logger disabled through its `Logger.disabled` flag or
    the global `logging.disable()` threshold could therefore still
    call `pformat()` before `Logger.isEnabledFor()` discarded the
    record.

    Exercise effective-level, per-logger, and global suppression
    independently. A poisoned `_chan.pformat()` proves rendering is
    skipped, while the fake transport proves `Channel.send()` still
    transmits the original payload and traceback-hiding flag.

    '''
    sent: list[tuple[object, bool]] = []

    class FakeTransport:
        async def send(
            self,
            payload: object,
            hide_tb: bool = False,
        ) -> None:
            sent.append((payload, hide_tb))

    def fail_pformat(payload: object) -> str:
        raise AssertionError(
            f'suppressed log rendered payload: {payload!r}'
        )

    chan_log = log.get_logger(
        name=f'guard_test.{suppression}',
    )
    std_log = chan_log.logger
    orig_level: int = std_log.level
    orig_disable: int = logging.root.manager.disable
    transport_level: int = log.CUSTOM_LEVELS['TRANSPORT']

    monkeypatch.setattr(_chan, 'log', chan_log)
    monkeypatch.setattr(_chan, 'pformat', fail_pformat)
    try:
        logging.disable(logging.NOTSET)
        std_log.setLevel(transport_level)

        if suppression == 'level':
            std_log.setLevel(logging.INFO)
        elif suppression == 'logger':
            monkeypatch.setattr(std_log, 'disabled', True)
        else:
            logging.disable(logging.CRITICAL)

        assert not chan_log.isEnabledFor(transport_level)

        transport = FakeTransport()
        chan = _chan.Channel(transport=transport)
        payload = object()

        async def send_payload() -> None:
            await chan.send(
                payload,
                hide_tb=True,
            )

        trio.run(send_payload)
        assert sent == [(payload, True)]
    finally:
        std_log.setLevel(orig_level)
        logging.disable(orig_disable)


# TODO, moar tests against existing feats:
# ------ - ------
# - [ ] color settings?
# - [ ] header contents like,
#   - actor + thread + task names from various conc-primitives,
# - [ ] `StackLevelAdapter` extensions,
#   - our custom levels/methods: `transport|runtime|cance|pdb|devx`
# - [ ] custom-headers support?
#

# TODO, test driven dev of new-ideas/long-wanted feats,
# ------ - ------
# - [ ] https://github.com/goodboy/tractor/issues/244
#  - [ ] @catern mentioned using a sync / deterministic sys
#       and in particular `svlogd`?
#       |_ https://smarden.org/runit/svlogd.8

# - [ ] using adapter vs. filters?
#    - https://stackoverflow.com/questions/60691759/add-information-to-every-log-message-in-python-logging/61830838#61830838

# - [ ] `.at_least_level()` optimization which short circuits wtv
#      `logging` is doing behind the scenes when the level filters
#      the emission..?

# - [ ] use of `.log.get_console_log()` in subactors and the
#    subtleties of ensuring it actually emits from a subproc.

# - [ ] this idea of activating per-subsys emissions with some
#    kind of `.name` filter passed to the runtime or maybe configured
#    via the root `StackLevelAdapter`?

# - [ ] use of `logging.dict.dictConfig()` to simplify the impl
#      of any of ^^ ??
#    - https://stackoverflow.com/questions/7507825/where-is-a-complete-example-of-logging-config-dictconfig
#    - https://docs.python.org/3/library/logging.config.html#configuration-dictionary-schema
#    - https://docs.python.org/3/library/logging.config.html#logging.config.dictConfig
