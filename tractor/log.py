"""
Log like a forester!
"""
import sys
from functools import partial
import logging
import colorlog  # type: ignore
from typing import Optional

from ._state import ActorContextInfo


_proj_name = 'tractor'
_default_loglevel = 'ERROR'

# Super sexy formatting thanks to ``colorlog``.
# (NOTE: we use the '{' format style)
# Here, `thin_white` is just the layperson's gray.
LOG_FORMAT = (
    # "{bold_white}{log_color}{asctime}{reset}"
    "{log_color}{asctime}{reset}"
    " {bold_white}{thin_white}({reset}"
    "{thin_white}{actor}, {process}, {task}){reset}{bold_white}{thin_white})"
    " {reset}{log_color}[{reset}{bold_log_color}{levelname}{reset}{log_color}]"
    " {log_color}{name}"
    " {thin_white}{filename}{log_color}:{reset}{thin_white}{lineno}{log_color}"
    " {reset}{bold_white}{thin_white}{message}"
)

DATE_FORMAT = '%b %d %H:%M:%S'

LEVELS = {
    'TRANSPORT': 5,
    'RUNTIME': 15,
    'PDB': 500,
}

STD_PALETTE = {
    'CRITICAL': 'red',
    'ERROR': 'red',
    'PDB': 'white',
    'WARNING': 'yellow',
    'INFO': 'green',
    'RUNTIME': 'white',
    'DEBUG': 'white',
    'TRANSPORT': 'cyan',
}

BOLD_PALETTE = {
    'bold': {
        level: f"bold_{color}" for level, color in STD_PALETTE.items()}
}


class StackLevelAdapter(logging.LoggerAdapter):

    def transport(
        self,
        msg: str,

    ) -> None:
        return self.log(5, msg)

    def runtime(
        self,
        msg: str,
    ) -> None:
        return self.log(15, msg)

    def pdb(
        self,
        msg: str,
    ) -> None:
        return self.log(500, msg)


def get_logger(

    name: str = None,
    _root_name: str = _proj_name,

) -> StackLevelAdapter:
    '''Return the package log or a sub-logger for ``name`` if provided.

    '''
    log = rlog = logging.getLogger(_root_name)

    if name and name != _proj_name:

        # handling for modules that use ``get_logger(__name__)`` to
        # avoid duplicate project-package token in msg output
        rname, _, tail = name.partition('.')
        if rname == _root_name:
            name = tail

        log = rlog.getChild(name)
        log.level = rlog.level

    # add our actor-task aware adapter which will dynamically look up
    # the actor and task names at each log emit
    logger = StackLevelAdapter(log, ActorContextInfo())

    # additional levels
    for name, val in LEVELS.items():
        logging.addLevelName(val, name)

        # ensure customs levels exist as methods
        assert getattr(logger, name.lower()), f'Logger does not define {name}'

    return logger


def get_console_log(
    level: str = None,
    **kwargs,
) -> logging.LoggerAdapter:
    '''Get the package logger and enable a handler which writes to stderr.

    Yeah yeah, i know we can use ``DictConfig``. You do it.
    '''
    log = get_logger(**kwargs)  # our root logger
    logger = log.logger

    if not level:
        return log

    log.setLevel(level.upper() if not isinstance(level, int) else level)

    if not any(
        handler.stream == sys.stderr  # type: ignore
        for handler in logger.handlers if getattr(handler, 'stream', None)
    ):
        handler = logging.StreamHandler()
        formatter = colorlog.ColoredFormatter(
            LOG_FORMAT,
            datefmt=DATE_FORMAT,
            log_colors=STD_PALETTE,
            secondary_log_colors=BOLD_PALETTE,
            style='{',
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return log


def get_loglevel() -> Optional[str]:
    return _default_loglevel
