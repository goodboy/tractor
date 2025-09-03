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

"""
Log like a forester!

"""
from collections.abc import Mapping
import sys
import logging
from logging import (
    LoggerAdapter,
    Logger,
    StreamHandler,
)
import colorlog  # type: ignore

import trio

from ._state import current_actor


_proj_name: str = 'tractor'
_default_loglevel: str = 'ERROR'

# Super sexy formatting thanks to ``colorlog``.
# (NOTE: we use the '{' format style)
# Here, `thin_white` is just the layperson's gray.
LOG_FORMAT = (
    # "{bold_white}{log_color}{asctime}{reset}"
    "{log_color}{asctime}{reset}"
    " {bold_white}{thin_white}({reset}"
    "{thin_white}{actor_name}[{actor_uid}], "
    "{process}, {task}){reset}{bold_white}{thin_white})"
    " {reset}{log_color}[{reset}{bold_log_color}{levelname}{reset}{log_color}]"
    " {log_color}{name}"
    " {thin_white}{filename}{log_color}:{reset}{thin_white}{lineno}{log_color}"
    " {reset}{bold_white}{thin_white}{message}"
)

DATE_FORMAT = '%b %d %H:%M:%S'

# FYI, ERROR is 40
# TODO: use a `bidict` to avoid the :155 check?
CUSTOM_LEVELS: dict[str, int] = {
    'TRANSPORT': 5,
    'RUNTIME': 15,
    'DEVX': 17,
    'CANCEL': 22,
    'PDB': 500,
}
STD_PALETTE = {
    'CRITICAL': 'red',
    'ERROR': 'red',
    'PDB': 'white',
    'DEVX': 'cyan',
    'WARNING': 'yellow',
    'INFO': 'green',
    'CANCEL': 'yellow',
    'RUNTIME': 'white',
    'DEBUG': 'white',
    'TRANSPORT': 'cyan',
}

BOLD_PALETTE = {
    'bold': {
        level: f"bold_{color}" for level, color in STD_PALETTE.items()}
}


def at_least_level(
    log: Logger|LoggerAdapter,
    level: int|str,
) -> bool:
    '''
    Predicate to test if a given level is active.

    '''
    if isinstance(level, str):
        level: int = CUSTOM_LEVELS[level.upper()]

    if log.getEffectiveLevel() <= level:
        return True
    return False


# TODO, compare with using a "filter" instead?
# - https://stackoverflow.com/questions/60691759/add-information-to-every-log-message-in-python-logging/61830838#61830838
#  |_corresponding dict-config,
#    https://stackoverflow.com/questions/7507825/where-is-a-complete-example-of-logging-config-dictconfig/7507842#7507842
#  - [ ] what's the benefit/tradeoffs?
#
class StackLevelAdapter(LoggerAdapter):
    '''
    A (software) stack oriented logger "adapter".

    '''
    def at_least_level(
        self,
        level: str,
    ) -> bool:
        return at_least_level(
            log=self,
            level=level,
        )

    def transport(
        self,
        msg: str,

    ) -> None:
        '''
        IPC transport level msg IO; generally anything below
        `.ipc.Channel` and friends.

        '''
        return self.log(5, msg)

    def runtime(
        self,
        msg: str,
    ) -> None:
        return self.log(15, msg)

    def cancel(
        self,
        msg: str,
    ) -> None:
        '''
        Cancellation sequencing, mostly for runtime reporting.

        '''
        return self.log(
            level=22,
            msg=msg,
            # stacklevel=4,
        )

    def pdb(
        self,
        msg: str,
    ) -> None:
        '''
        `pdb`-REPL (debugger) related statuses.

        '''
        return self.log(500, msg)

    def devx(
        self,
        msg: str,
    ) -> None:
        '''
        "Developer experience" sub-sys statuses.

        '''
        return self.log(17, msg)

    def log(
        self,
        level,
        msg,
        *args,
        **kwargs,
    ):
        '''
        Delegate a log call to the underlying logger, after adding
        contextual information from this adapter instance.

        NOTE: all custom level methods (above) delegate to this!

        '''
        if self.isEnabledFor(level):
            stacklevel: int = 3
            if (
                level in CUSTOM_LEVELS.values()
            ):
                stacklevel: int = 4

            # msg, kwargs = self.process(msg, kwargs)
            self._log(
                level=level,
                msg=msg,
                args=args,
                # NOTE: not sure how this worked before but, it
                # seems with our custom level methods defined above
                # we do indeed (now) require another stack level??
                stacklevel=stacklevel,
                **kwargs,
            )

    # LOL, the stdlib doesn't allow passing through ``stacklevel``..
    def _log(
        self,
        level,
        msg,
        args,
        exc_info=None,
        extra=None,
        stack_info=False,

        # XXX: bit we added to show fileinfo from actual caller.
        # - this level
        # - then ``.log()``
        # - then finally the caller's level..
        stacklevel=4,
    ):
        '''
        Low-level log implementation, proxied to allow nested logger adapters.

        '''
        return self.logger._log(
            level,
            msg,
            args,
            exc_info=exc_info,
            extra=self.extra,
            stack_info=stack_info,
            stacklevel=stacklevel,
        )


# TODO IDEAs:
# -[ ] move to `.devx.pformat`?
# -[ ] do per task-name and actor-name color coding
# -[ ] unique color per task-id and actor-uuid
def pformat_task_uid(
    id_part: str = 'tail'
):
    '''
    Return `str`-ified unique for a `trio.Task` via a combo of its
    `.name: str` and `id()` truncated output.

    '''
    task: trio.Task = trio.lowlevel.current_task()
    tid: str = str(id(task))
    if id_part == 'tail':
        tid_part: str = tid[-6:]
    else:
        tid_part: str = tid[:6]

    return f'{task.name}[{tid_part}]'


_conc_name_getters = {
    'task': pformat_task_uid,
    'actor': lambda: current_actor(),
    'actor_name': lambda: current_actor().name,
    'actor_uid': lambda: current_actor().uid[1][:6],
}


class ActorContextInfo(Mapping):
    '''
    Dyanmic lookup for local actor and task names.

    '''
    _context_keys = (
        'task',
        'actor',
        'actor_name',
        'actor_uid',
    )

    def __len__(self):
        return len(self._context_keys)

    def __iter__(self):
        return iter(self._context_keys)

    def __getitem__(self, key: str) -> str:
        try:
            return _conc_name_getters[key]()
        except RuntimeError:
            # no local actor/task context initialized yet
            return f'no {key} context'


def get_logger(
    # ?TODO, could we just grab the caller's `mod.__name__`?
    # -[ ] do it with `inspect` says SO,
    # |_https://stackoverflow.com/a/1095621
    name: str|None = None,
    _root_name: str = _proj_name,

    logger: Logger|None = None,

    # TODO, using `.config.dictConfig()` api?
    # -[ ] SO answer with docs links
    #  |_https://stackoverflow.com/questions/7507825/where-is-a-complete-example-of-logging-config-dictconfig
    #  |_https://docs.python.org/3/library/logging.config.html#configuration-dictionary-schema
    subsys_spec: str|None = None,

) -> StackLevelAdapter:
    '''
    Return the `tractor`-library root logger or a sub-logger for
    `name` if provided.

    '''
    log: Logger
    log = rlog = logger or logging.getLogger(_root_name)

    if (
        name != _proj_name
        and
        name
        # ^TODO? see caller_mod.__name__ as default above?
    ):

        # NOTE: for handling for modules that use `get_logger(__name__)`
        # we make the following stylistic choice:
        # - always avoid duplicate project-package token
        #   in msg output: i.e. tractor.tractor.ipc._chan.py in header
        #   looks ridiculous XD
        # - never show the leaf module name in the {name} part
        #   since in python the {filename} is always this same
        #   module-file.

        sub_name: None|str = None

        # ex. modden.runtime.progman
        # -> rname='modden', _, sub_name='runtime.progman'
        rname, _, sub_name = name.partition('.')

        # ex. modden.runtime.progman
        # -> pkgpath='runtime', _, leaf_mod='progman'
        subpkg_path, _, leaf_mod = sub_name.rpartition('.')

        # NOTE: special usage for passing `name=__name__`,
        #
        # - remove duplication of any root-pkg-name in the
        #   (sub/child-)logger name; i.e. never include the
        #   `_root_name` *twice* in the top-most-pkg-name/level
        #
        # -> this happens normally since it is added to `.getChild()`
        #   and as the name of its root-logger.
        #
        # => So for ex. (module key in the name) something like
        #   `name='tractor.trionics._broadcast` is passed,
        #   only includes the first 2 sub-pkg name-tokens in the
        #   child-logger's name; the colored "pkg-namespace" header
        #   will then correctly show the same value as `name`.
        if rname == _root_name:
            sub_name = subpkg_path

        # XXX, do some double-checks for duplication of,
        # - root-pkg-name, already in root logger
        # - leaf-module-name already in `{filename}` header-field
        if _root_name in sub_name:
            _duplicate, _, sub_name = sub_name.partition('.')
            if _duplicate:
                assert _duplicate == rname
                log.warning(
                    f'Duplicate pkg-name in sub-logger key?\n'
                    f'_root_name = {_root_name!r}\n'
                    f'sub_name = {sub_name!r}\n'
                )

        if leaf_mod in sub_name:
            # XXX, for debuggin..
            # import pdbp; pdbp.set_trace()
            log.warning(
                f'Duplicate leaf-module-name in sub-logger key?\n'
                f'leaf_mod = {leaf_mod!r}\n'
                f'sub_name = {sub_name!r}\n'
            )

        if not sub_name:
            log = rlog
        else:
            log = rlog.getChild(sub_name)

        log.level = rlog.level

    # add our actor-task aware adapter which will dynamically look up
    # the actor and task names at each log emit
    logger = StackLevelAdapter(
        log,
        ActorContextInfo(),
    )

    # additional levels
    for name, val in CUSTOM_LEVELS.items():
        logging.addLevelName(val, name)

        # ensure customs levels exist as methods
        assert getattr(logger, name.lower()), f'Logger does not define {name}'

    return logger


def get_console_log(
    level: str|None = None,
    logger: Logger|StackLevelAdapter|None = None,
    **kwargs,

) -> LoggerAdapter:
    '''
    Get a `tractor`-style logging instance: a `Logger` wrapped in
    a `StackLevelAdapter` which injects various concurrency-primitive
    (process, thread, task) fields and enables a `StreamHandler` that
    writes on stderr using `colorlog` formatting.

    Yeah yeah, i know we can use `logging.config.dictConfig()`. You do it.

    '''
    # get/create a stack-aware-adapter
    if (
        logger
        and
        isinstance(logger, StackLevelAdapter)
    ):
        # XXX, for ex. when passed in by a caller wrapping some
        # other lib's logger instance with our level-adapter.
        log = logger

    else:
        log: StackLevelAdapter = get_logger(
            logger=logger,
            **kwargs
        )

    logger: Logger|StackLevelAdapter = log.logger
    if not level:
        return log

    log.setLevel(
        level.upper()
        if not isinstance(level, int)
        else level
    )

    if not any(
        handler.stream == sys.stderr  # type: ignore
        for handler in logger.handlers if getattr(
            handler,
            'stream',
            None,
        )
    ):
        fmt: str = LOG_FORMAT  # always apply our format?
        handler = StreamHandler()
        formatter = colorlog.ColoredFormatter(
            fmt=fmt,
            datefmt=DATE_FORMAT,
            log_colors=STD_PALETTE,
            secondary_log_colors=BOLD_PALETTE,
            style='{',
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return log


def get_loglevel() -> str:
    return _default_loglevel


# global module logger for tractor itself
log: StackLevelAdapter = get_logger('tractor')
