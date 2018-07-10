"""
Log like a forester!
"""
from functools import partial
import sys
import logging
import colorlog

_proj_name = 'tractor'

# Super sexy formatting thanks to ``colorlog``.
# (NOTE: we use the '{' format style)
# Here, `thin_white` is just the layperson's gray.
LOG_FORMAT = (
    # "{bold_white}{log_color}{asctime}{reset}"
    "{log_color}{asctime}{reset}"
    " {bold_white}{thin_white}({reset}"
    "{thin_white}{processName}: {threadName}{reset}{bold_white}{thin_white})"
    " {reset}{log_color}[{reset}{bold_log_color}{levelname}{reset}{log_color}]"
    " {log_color}{name}"
    " {thin_white}{filename}{log_color}:{reset}{thin_white}{lineno}{log_color}"
    " {reset}{bold_white}{thin_white}{message}"
)
DATE_FORMAT = '%b %d %H:%M:%S'
LEVELS = {
    'GARBAGE': 1,
    'TRACE': 5,
    'PROFILE': 15,
    'QUIET': 1000,
}
STD_PALETTE = {
    'CRITICAL': 'red',
    'ERROR': 'red',
    'WARNING': 'yellow',
    'INFO': 'green',
    'DEBUG': 'white',
    'TRACE': 'cyan',
    'GARBAGE': 'blue',
}
BOLD_PALETTE = {
    'bold': {
        level: f"bold_{color}" for level, color in STD_PALETTE.items()}
}


def get_logger(name: str = None) -> logging.Logger:
    '''Return the package log or a sub-log for `name` if provided.
    '''
    log = rlog = logging.getLogger(_proj_name)
    if name and name != _proj_name:
        log = rlog.getChild(name)
        log.level = rlog.level

    # additional levels
    for name, val in LEVELS.items():
        logging.addLevelName(val, name)
        # ex. create ``log.trace()``
        setattr(log, name.lower(), partial(log.log, val))

    return log


def get_console_log(level: str = None, name: str = None) -> logging.Logger:
    '''Get the package logger and enable a handler which writes to stderr.

    Yeah yeah, i know we can use ``DictConfig``. You do it...
    '''
    log = get_logger(name)  # our root logger

    if not level:
        return

    log.setLevel(level.upper() if not isinstance(level, int) else level)

    if not any(
        handler.stream == sys.stderr for handler in log.handlers
        if getattr(handler, 'stream', None)
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
        log.addHandler(handler)

    return log
