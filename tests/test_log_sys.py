'''
`tractor.log`-wrapping unit tests.

'''
import importlib
from pathlib import Path
import shutil
import sys
from types import ModuleType

import pytest
import tractor


def test_root_pkg_not_duplicated_in_logger_name():
    '''
    When both `_root_name` and `name` are passed and they have
    a common `<root_name>.< >` prefix, ensure that it is not
    duplicated in the child's `StackLevelAdapter.name: str`.

    '''
    project_name: str = 'pylib'
    pkg_path: str = 'pylib.subpkg.mod'

    proj_log = tractor.log.get_logger(
        _root_name=project_name,
        mk_sublog=False,
    )

    sublog = tractor.log.get_logger(
        _root_name=project_name,
        name=pkg_path,
    )

    assert proj_log is not sublog
    assert sublog.name.count(proj_log.name) == 1
    assert 'mod' not in sublog.name


# ?TODO, move this into internal libs?
# -[ ] we already use it in `modden.config._pymod` as well
def load_module_from_path(
    path: Path,
    module_name: str|None = None,
) -> ModuleType:
    '''
    Taken from SO,
    https://stackoverflow.com/a/67208147

    which is based on stdlib docs,
    https://docs.python.org/3/library/importlib.html#importing-a-source-file-directly

    '''
    module_name = module_name or path.stem
    spec = importlib.util.spec_from_file_location(
        module_name,
        str(path),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_implicit_mod_name_applied_for_child(
    testdir: pytest.Pytester,
):
    '''
    Verify that when `.log.get_logger(pkg_name='pylib')` is called
    from a given sub-mod from within the `pylib` pkg-path, we
    implicitly set the equiv of `name=__name__` from the caller's
    module.

    '''
    proj_name: str = 'snakelib'
    mod_code: str = (
        f'import tractor\n'
        f'\n'
        f'log = tractor.log.get_logger(_root_name="{proj_name}")\n'
    )

    # create a sub-module for each pkg layer
    _lib = testdir.mkpydir(proj_name)
    pkg: Path = Path(_lib)
    subpkg: Path = pkg / 'subpkg'
    subpkg.mkdir()

    pkgmod: Path = subpkg / "__init__.py"
    pkgmod.touch()

    _submod: Path = testdir.makepyfile(
        _mod=mod_code,
    )

    pkg_mod = pkg / 'mod.py'
    pkg_subpkg_submod = subpkg / 'submod.py'
    shutil.copyfile(
        _submod,
        pkg_mod,
    )
    shutil.copyfile(
        _submod,
        pkg_subpkg_submod,
    )
    testdir.chdir()

    # XXX NOTE, once the "top level" pkg mod has been
    # imported, we can then use `import` syntax to
    # import it's sub-pkgs and modules.
    pkgmod = load_module_from_path(
        Path(pkg / '__init__.py'),
        module_name=proj_name,
    )
    pkg_root_log = tractor.log.get_logger(
        _root_name=proj_name,
        mk_sublog=False,
    )
    assert pkg_root_log.name == proj_name
    assert not pkg_root_log.logger.getChildren()

    from snakelib import mod
    assert mod.log.name == proj_name

    from snakelib.subpkg import submod
    assert (
        submod.log.name
        ==
        submod.__package__  # ?TODO, use this in `.get_logger()` instead?
        ==
        f'{proj_name}.subpkg'
    )

    sub_logs = pkg_root_log.logger.getChildren()
    assert len(sub_logs) == 1  # only one nested sub-pkg module
    assert submod.log.logger in sub_logs

    # breakpoint()


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
