'''
`tractor.log`-wrapping unit tests.

'''
import tractor


def test_root_pkg_not_duplicated():

    project_name: str = 'pylib'
    pkg_path: str = 'pylib.subpkg.mod'

    log = tractor.log.get_logger(
        _root_name=project_name,
    )

    sublog = tractor.log.get_logger(
        _root_name=project_name,
        name=pkg_path,
    )

    assert log is not sublog
    assert sublog.name.count(log.name) == 1
    assert 'mod' not in sublog.name


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
