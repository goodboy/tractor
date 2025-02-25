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
Runtime "developer experience" utils and addons to aid our
(advanced) users and core devs in building distributed applications
and working with/on the actor runtime.

"""
from ._debug import (
    maybe_wait_for_debugger as maybe_wait_for_debugger,
    acquire_debug_lock as acquire_debug_lock,
    breakpoint as breakpoint,
    pause as pause,
    pause_from_sync as pause_from_sync,
    shield_sigint_handler as shield_sigint_handler,
    MultiActorPdb as MultiActorPdb,
    open_crash_handler as open_crash_handler,
    maybe_open_crash_handler as maybe_open_crash_handler,
    post_mortem as post_mortem,
)
from ._stackscope import (
    enable_stack_on_sig as enable_stack_on_sig,
)
from .pformat import (
    add_div as add_div,
    pformat_caller_frame as pformat_caller_frame,
    pformat_boxed_tb as pformat_boxed_tb,
)


def _enable_readline_feats() -> str:
    '''
    Handle `readline` when compiled with `libedit` to avoid breaking
    tab completion in `pdbp` (and its dep `tabcompleter`)
    particularly since `uv` cpython distis are compiled this way..

    See docs for deats,
    https://docs.python.org/3/library/readline.html#module-readline

    Originally discovered soln via SO answer,
    https://stackoverflow.com/q/49287102

    '''
    import readline
    if (
        # 3.13+ attr
        # https://docs.python.org/3/library/readline.html#readline.backend
        (getattr(readline, 'backend', False) == 'libedit')
        or
        'libedit' in readline.__doc__
    ):
        readline.parse_and_bind("python:bind -v")
        readline.parse_and_bind("python:bind ^I rl_complete")
        return 'libedit'
    else:
        readline.parse_and_bind("tab: complete")
        readline.parse_and_bind("set editing-mode vi")
        readline.parse_and_bind("set keymap vi")
        return 'readline'


# TODO, move this to a new `.devx._pdbp` mod?
_enable_readline_feats()
