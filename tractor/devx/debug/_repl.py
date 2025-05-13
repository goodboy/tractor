# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public License
# as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.

'''
`pdpp.Pdb` extentions/customization and other delegate usage.

'''
from functools import (
    cached_property,
)
import os

import pdbp
from tractor._state import (
    is_root_process,
)

from ._tty_lock import (
    Lock,
    DebugStatus,
)


class TractorConfig(pdbp.DefaultConfig):
    '''
    Custom `pdbp` config which tries to use the best tradeoff
    between pretty and minimal.

    '''
    use_pygments: bool = True
    sticky_by_default: bool = False
    enable_hidden_frames: bool = True

    # much thanks @mdmintz for the hot tip!
    # fixes line spacing issue when resizing terminal B)
    truncate_long_lines: bool = False

    # ------ - ------
    # our own custom config vars mostly
    # for syncing with the actor tree's singleton
    # TTY `Lock`.


class PdbREPL(pdbp.Pdb):
    '''
    Add teardown hooks and local state describing any
    ongoing TTY `Lock` request dialog.

    '''
    # override the pdbp config with our coolio one
    # NOTE: this is only loaded when no `~/.pdbrc` exists
    # so we should prolly pass it into the .__init__() instead?
    # i dunno, see the `DefaultFactory` and `pdb.Pdb` impls.
    DefaultConfig = TractorConfig

    status = DebugStatus

    # NOTE: see details in stdlib's `bdb.py`
    # def user_exception(self, frame, exc_info):
    #     '''
    #     Called when we stop on an exception.
    #     '''
    #     log.warning(
    #         'Exception during REPL sesh\n\n'
    #         f'{frame}\n\n'
    #         f'{exc_info}\n\n'
    #     )

    # NOTE: this actually hooks but i don't see anyway to detect
    # if an error was caught.. this is why currently we just always
    # call `DebugStatus.release` inside `_post_mortem()`.
    # def preloop(self):
    #     print('IN PRELOOP')
    #     super().preloop()

    # TODO: cleaner re-wrapping of all this?
    # -[ ] figure out how to disallow recursive .set_trace() entry
    #     since that'll cause deadlock for us.
    # -[ ] maybe a `@cm` to call `super().<same_meth_name>()`?
    # -[ ] look at hooking into the `pp` hook specially with our
    #     own set of pretty-printers?
    #    * `.pretty_struct.Struct.pformat()`
    #    * `.pformat(MsgType.pld)`
    #    * `.pformat(Error.tb_str)`?
    #    * .. maybe more?
    #
    def set_continue(self):
        try:
            super().set_continue()
        finally:
            # NOTE: for subactors the stdio lock is released via the
            # allocated RPC locker task, so for root we have to do it
            # manually.
            if (
                is_root_process()
                and
                Lock._debug_lock.locked()
                and
                DebugStatus.is_main_trio_thread()
            ):
                # Lock.release(raise_on_thread=False)
                Lock.release()

            # XXX AFTER `Lock.release()` for root local repl usage
            DebugStatus.release()

    def set_quit(self):
        try:
            super().set_quit()
        finally:
            if (
                is_root_process()
                and
                Lock._debug_lock.locked()
                and
                DebugStatus.is_main_trio_thread()
            ):
                # Lock.release(raise_on_thread=False)
                Lock.release()

            # XXX after `Lock.release()` for root local repl usage
            DebugStatus.release()

    # XXX NOTE: we only override this because apparently the stdlib pdb
    # bois likes to touch the SIGINT handler as much as i like to touch
    # my d$%&.
    def _cmdloop(self):
        self.cmdloop()

    @cached_property
    def shname(self) -> str | None:
        '''
        Attempt to return the login shell name with a special check for
        the infamous `xonsh` since it seems to have some issues much
        different from std shells when it comes to flushing the prompt?

        '''
        # SUPER HACKY and only really works if `xonsh` is not used
        # before spawning further sub-shells..
        shpath = os.getenv('SHELL', None)

        if shpath:
            if (
                os.getenv('XONSH_LOGIN', default=False)
                or 'xonsh' in shpath
            ):
                return 'xonsh'

            return os.path.basename(shpath)

        return None


def mk_pdb() -> PdbREPL:
    '''
    Deliver a new `PdbREPL`: a multi-process safe `pdbp.Pdb`-variant
    using the magic of `tractor`'s SC-safe IPC.

    B)

    Our `pdb.Pdb` subtype accomplishes multi-process safe debugging
    by:

    - mutexing access to the root process' std-streams (& thus parent
      process TTY) via an IPC managed `Lock` singleton per
      actor-process tree.

    - temporarily overriding any subactor's SIGINT handler to shield
      during live REPL sessions in sub-actors such that cancellation
      is never (mistakenly) triggered by a ctrl-c and instead only by
      explicit runtime API requests or after the
      `pdb.Pdb.interaction()` call has returned.

    FURTHER, the `pdbp.Pdb` instance is configured to be `trio`
    "compatible" from a SIGINT handling perspective; we mask out
    the default `pdb` handler and instead apply `trio`s default
    which mostly addresses all issues described in:

     - https://github.com/python-trio/trio/issues/1155

    The instance returned from this factory should always be
    preferred over the default `pdb[p].set_trace()` whenever using
    a `pdb` REPL inside a `trio` based runtime.

    '''
    pdb = PdbREPL()

    # XXX: These are the important flags mentioned in
    # https://github.com/python-trio/trio/issues/1155
    # which resolve the traceback spews to console.
    pdb.allow_kbdint = True
    pdb.nosigint = True
    return pdb
