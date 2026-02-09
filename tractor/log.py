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
An enhanced logging subsys.

An extended logging layer using (for now) the stdlib's `logging`
+ `colorlog` which embeds concurrency-primitive/runtime info into
records (headers) to help you better grok your distributed systems
built on `tractor`.


'''
from collections.abc import Mapping
from functools import partial
from inspect import (
    FrameInfo,
    getmodule,
    stack,
)
import sys
import logging
from logging import (
    LoggerAdapter,
    Logger,
    StreamHandler,
)
from types import ModuleType
import warnings

import colorlog  # type: ignore
import trio

from ._state import current_actor


_default_loglevel: str = 'ERROR'

# Super sexy formatting thanks to ``colorlog``.
# (NOTE: we use the '{' format style)
# Here, `thin_white` is just the layperson's gray.
LOG_FORMAT: str = (
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

DATE_FORMAT: str = '%b %d %H:%M:%S'

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

BOLD_PALETTE: dict[
    str,
    dict[int, str],
] = {
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
    @property
    def level(self) -> str:
        '''
        The currently set `str` emit level (in lowercase).

        '''
        return logging.getLevelName(
            self.getEffectiveLevel()
        ).lower()

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


_curr_actor_no_exc = partial(
    current_actor,
    err_on_no_runtime=False,
)

_conc_name_getters = {
    'task': pformat_task_uid,
    'actor': lambda: _curr_actor_no_exc(),
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


_proj_name: str = 'tractor'


def get_logger(
    name: str|None = None,
    # ^NOTE, setting `name=_proj_name=='tractor'` enables the "root
    # logger" for `tractor` itself.
    pkg_name: str = _proj_name,
    # XXX, deprecated, use ^
    _root_name: str|None = None,

    logger: Logger|None = None,

    # TODO, using `.config.dictConfig()` api?
    # -[ ] SO answer with docs links
    #  |_https://stackoverflow.com/questions/7507825/where-is-a-complete-example-of-logging-config-dictconfig
    #  |_https://docs.python.org/3/library/logging.config.html#configuration-dictionary-schema
    subsys_spec: str|None = None,
    mk_sublog: bool = True,
    _strict_debug: bool = False,

) -> StackLevelAdapter:
    '''
    Return the `tractor`-library root logger or a sub-logger for
    `name` if provided.

    When `name` is left null we try to auto-detect the caller's
    `mod.__name__` and use that as a the sub-logger key.
    This allows for example creating a module level instance like,

    .. code:: python

        log = tractor.log.get_logger(_root_name='mylib')

    and by default all console record headers will show the caller's
    (of any `log.<level>()`-method) correct sub-pkg's
    + py-module-file.

    '''
    if _root_name:
        msg: str = (
            'The `_root_name: str` param of `get_logger()` is now deprecated.\n'
            'Use the new `pkg_name: str` instead, it is the same usage.\n'
        )
        warnings.warn(
            msg,
            DeprecationWarning,
            stacklevel=2,
        )

        pkg_name: str =  _root_name

    def get_caller_mod(
        frames_up:int = 2
    ):
        '''
        Attempt to get the module which called `tractor.get_logger()`.

        '''
        callstack: list[FrameInfo] = stack()
        caller_fi: FrameInfo = callstack[frames_up]
        caller_mod: ModuleType = getmodule(caller_fi.frame)
        return caller_mod

    # --- Auto--naming-CASE ---
    # -------------------------
    # Implicitly introspect the caller's module-name whenever `name`
    # if left as the null default.
    #
    # When the `pkg_name` is `in` in the `mod.__name__` we presume
    # this instance can be created as a sub-`StackLevelAdapter` and
    # that the intention is to get free module-path tracing and
    # filtering (well once we implement that) oriented around the
    # py-module code hierarchy of the consuming project.
    #
    if (
        mk_sublog
        and
        name is None
        and
        pkg_name
    ):
        if (caller_mod := get_caller_mod()):
            # ?XXX how is this `caller_mod.__name__` defined?
            # => well by how the mod is imported.. XD
            # |_https://stackoverflow.com/a/15883682
            #
            # if pkg_name in caller_mod.__package__:
            #     from tractor.devx.debug import mk_pdb
            #     mk_pdb().set_trace()

            mod_ns_path: str = caller_mod.__name__
            mod_pkg_ns_path: str = caller_mod.__package__
            # if 'snakelib' in mod_pkg_ns_path:
            #     import pdbp
            #     breakpoint()
            if (
                mod_pkg_ns_path in mod_ns_path
                or
                pkg_name in mod_ns_path
            ):
                # proper_mod_name = mod_ns_path.lstrip(
                proper_mod_name = mod_pkg_ns_path.removeprefix(
                    f'{pkg_name}.'
                )
                name = proper_mod_name

            elif (
                pkg_name
                # and
                # pkg_name in mod_ns_path
            ):
                name = mod_ns_path

            if _strict_debug:
                msg: str = (
                    f'@ {get_caller_mod()}\n'
                    f'Generating sub-logger name,\n'
                    f'{pkg_name}.{name}\n'
                )
                if _curr_actor_no_exc():
                    _root_log.debug(msg)
                elif pkg_name != _proj_name:
                    print(
                        f'=> tractor.log.get_logger():\n'
                        f'{msg}\n'
                    )

    # build a root logger instance
    log: Logger
    rlog = log = (
        logger
        or
        logging.getLogger(pkg_name)
    )

    # XXX, lowlevel debuggin..
    # if pkg_name != _proj_name:
        # from tractor.devx.debug import mk_pdb
        # mk_pdb().set_trace()

    # NOTE: for handling for modules that use the unecessary,
    # `get_logger(__name__)`
    #
    # we make the following stylistic choice:
    # - always avoid duplicate project-package token
    #   in msg output: i.e. tractor.tractor.ipc._chan.py in header
    #   looks ridiculous XD
    # - never show the leaf module name in the {name} part
    #   since in python the {filename} is always this same
    #   module-file.
    if (
        name
        and
        # ?TODO? more correct?
        # _proj_name not in name
        name != pkg_name
    ):
        # ex. modden.runtime.progman
        # -> rname='modden', _, pkg_path='runtime.progman'
        if (
            pkg_name
            and
            pkg_name in name
        ):
            proper_name: str = name.removeprefix(
                f'{pkg_name}.'
            )
            # if 'pylib' in name:
            #     import pdbp
            #     breakpoint()

            msg: str = (
                f'@ {get_caller_mod()}\n'
                f'Duplicate pkg-name in sub-logger `name`-key?\n'
                f'pkg_name = {pkg_name!r}\n'
                f'name = {name!r}\n'
                f'\n'
                f'=> You should change your input params to,\n'
                f'get_logger(\n' 
                f'    pkg_name={pkg_name!r}\n'
                f'    name={proper_name!r}\n'
                f')'
            )
            # assert _duplicate == rname
            if _curr_actor_no_exc():
                _root_log.warning(msg)
            else:
                print(
                    f'=> tractor.log.get_logger() ERROR:\n'
                    f'{msg}\n'
                )

            name = proper_name

        rname: str = pkg_name
        pkg_path: str = name


            # (
            #     rname,
            #     _,
            #     pkg_path,
            # ) = name.partition('.')

        # For ex. 'modden.runtime.progman'
        # -> pkgpath='runtime', _, leaf_mod='progman'
        (
            subpkg_path,
            _,
            leaf_mod,
        ) = pkg_path.rpartition('.')

        # NOTE: special usage for passing `name=__name__`,
        #
        # - remove duplication of any root-pkg-name in the
        #   (sub/child-)logger name; i.e. never include the
        #   `pkg_name` *twice* in the top-most-pkg-name/level
        #
        # -> this happens normally since it is added to `.getChild()`
        #   and as the name of its root-logger.
        #
        # => So for ex. (module key in the name) something like
        #   `name='tractor.trionics._broadcast` is passed,
        #   only includes the first 2 sub-pkg name-tokens in the
        #   child-logger's name; the colored "pkg-namespace" header
        #   will then correctly show the same value as `name`.
        if (
            # XXX, TRY to remove duplication cases
            # which get warn-logged on below!
            (
                # when, subpkg_path == pkg_path
                subpkg_path
                and
                rname == pkg_name
            )
            # ) or (
            #     # when, pkg_path == leaf_mod
            #     pkg_path
            #     and
            #     leaf_mod == pkg_path
            # )
        ):
            pkg_path = subpkg_path

        # XXX, do some double-checks for duplication of,
        # - root-pkg-name, already in root logger
        # - leaf-module-name already in `{filename}` header-field
        if (
            _strict_debug
            and
            pkg_name
            and
            pkg_name in pkg_path
        ):
            _duplicate, _, pkg_path = pkg_path.partition('.')
            if _duplicate:
                msg: str = (
                    f'@ {get_caller_mod()}\n'
                    f'Duplicate pkg-name in sub-logger key?\n'
                    f'pkg_name = {pkg_name!r}\n'
                    f'pkg_path = {pkg_path!r}\n'
                )
                # assert _duplicate == rname
                if _curr_actor_no_exc():
                    _root_log.warning(msg)
                else:
                    print(
                        f'=> tractor.log.get_logger() ERROR:\n'
                        f'{msg}\n'
                    )
                # XXX, should never get here?
                breakpoint()
        if (
            _strict_debug
            and
            leaf_mod
            and
            leaf_mod in pkg_path
        ):
            msg: str = (
                f'@ {get_caller_mod()}\n'
                f'Duplicate leaf-module-name in sub-logger key?\n'
                f'leaf_mod = {leaf_mod!r}\n'
                f'pkg_path = {pkg_path!r}\n'
            )
            if _curr_actor_no_exc():
                _root_log.warning(msg)
            else:
                print(
                    f'=> tractor.log.get_logger() ERROR:\n'
                    f'{msg}\n'
                )

        # mk/get underlying (sub-)`Logger`
        if (
            not pkg_path
            and
            leaf_mod == pkg_name
        ):
            # breakpoint()
            log = rlog

        elif mk_sublog:
            # breakpoint()
            log = rlog.getChild(pkg_path)

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

        # ensure our custom adapter levels exist as methods
        assert getattr(
            logger,
            name.lower()
        ), (
            f'Logger does not define {name}'
        )

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
_root_log: StackLevelAdapter = get_logger('tractor')
