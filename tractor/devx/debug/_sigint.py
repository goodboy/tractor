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
A custom SIGINT handler which mainly shields actor (task)
cancellation during REPL interaction.

'''
from __future__ import annotations
from typing import (
    TYPE_CHECKING,
)
import trio
from tractor.log import get_logger
from tractor._state import (
    current_actor,
    is_root_process,
)
from ._repl import (
    PdbREPL,
)
from ._tty_lock import (
    any_connected_locker_child,
    DebugStatus,
    Lock,
)

if TYPE_CHECKING:
    from tractor.ipc import (
        Channel,
    )
    from tractor._runtime import (
        Actor,
    )

log = get_logger()

_ctlc_ignore_header: str = (
    'Ignoring SIGINT while debug REPL in use'
)


def sigint_shield(
    signum: int,
    frame: 'frame',  # type: ignore # noqa
    *args,

) -> None:
    '''
    Specialized, debugger-aware SIGINT handler.

    In childred we always ignore/shield for SIGINT to avoid
    deadlocks since cancellation should always be managed by the
    supervising parent actor. The root actor-proces is always
    cancelled on ctrl-c.

    '''
    __tracebackhide__: bool = True
    actor: Actor = current_actor()

    def do_cancel():
        # If we haven't tried to cancel the runtime then do that instead
        # of raising a KBI (which may non-gracefully destroy
        # a ``trio.run()``).
        if not actor._cancel_called:
            actor.cancel_soon()

        # If the runtime is already cancelled it likely means the user
        # hit ctrl-c again because teardown didn't fully take place in
        # which case we do the "hard" raising of a local KBI.
        else:
            raise KeyboardInterrupt

    # only set in the actor actually running the REPL
    repl: PdbREPL|None = DebugStatus.repl

    # TODO: maybe we should flatten out all these cases using
    # a match/case?
    #
    # root actor branch that reports whether or not a child
    # has locked debugger.
    if is_root_process():
        # log.warning(
        log.devx(
            'Handling SIGINT in root actor\n'
            f'{Lock.repr()}'
            f'{DebugStatus.repr()}\n'
        )
        # try to see if the supposed (sub)actor in debug still
        # has an active connection to *this* actor, and if not
        # it's likely they aren't using the TTY lock / debugger
        # and we should propagate SIGINT normally.
        any_connected: bool = any_connected_locker_child()

        problem = (
            f'root {actor.uid} handling SIGINT\n'
            f'any_connected: {any_connected}\n\n'

            f'{Lock.repr()}\n'
        )

        if (
            (ctx := Lock.ctx_in_debug)
            and
            (uid_in_debug := ctx.chan.uid) # "someone" is (ostensibly) using debug `Lock`
        ):
            name_in_debug: str = uid_in_debug[0]
            assert not repl
            # if not repl:  # but it's NOT us, the root actor.
            # sanity: since no repl ref is set, we def shouldn't
            # be the lock owner!
            assert name_in_debug != 'root'

            # IDEAL CASE: child has REPL as expected
            if any_connected:  # there are subactors we can contact
                # XXX: only if there is an existing connection to the
                # (sub-)actor in debug do we ignore SIGINT in this
                # parent! Otherwise we may hang waiting for an actor
                # which has already terminated to unlock.
                #
                # NOTE: don't emit this with `.pdb()` level in
                # root without a higher level.
                log.runtime(
                    _ctlc_ignore_header
                    +
                    f' by child '
                    f'{uid_in_debug}\n'
                )
                problem = None

            else:
                problem += (
                    '\n'
                    f'A `pdb` REPL is SUPPOSEDLY in use by child {uid_in_debug}\n'
                    f'BUT, no child actors are IPC contactable!?!?\n'
                )

        # IDEAL CASE: root has REPL as expected
        else:
            # root actor still has this SIGINT handler active without
            # an actor using the `Lock` (a bug state) ??
            # => so immediately cancel any stale lock cs and revert
            # the handler!
            if not DebugStatus.repl:
                # TODO: WHEN should we revert back to ``trio``
                # handler if this one is stale?
                # -[ ] maybe after a counts work of ctl-c mashes?
                # -[ ] use a state var like `stale_handler: bool`?
                problem += (
                    'No subactor is using a `pdb` REPL according `Lock.ctx_in_debug`?\n'
                    'BUT, the root should be using it, WHY this handler ??\n\n'
                    'So either..\n'
                    '- some root-thread is using it but has no `.repl` set?, OR\n'
                    '- something else weird is going on outside the runtime!?\n'
                )
            else:
                # NOTE: since we emit this msg on ctl-c, we should
                # also always re-print the prompt the tail block!
                log.pdb(
                    _ctlc_ignore_header
                    +
                    f' by root actor..\n'
                    f'{DebugStatus.repl_task}\n'
                    f' |_{repl}\n'
                )
                problem = None

        # XXX if one is set it means we ARE NOT operating an ideal
        # case where a child subactor or us (the root) has the
        # lock without any other detected problems.
        if problem:

            # detect, report and maybe clear a stale lock request
            # cancel scope.
            lock_cs: trio.CancelScope = Lock.get_locking_task_cs()
            maybe_stale_lock_cs: bool = (
                lock_cs is not None
                and not lock_cs.cancel_called
            )
            if maybe_stale_lock_cs:
                problem += (
                    '\n'
                    'Stale `Lock.ctx_in_debug._scope: CancelScope` detected?\n'
                    f'{Lock.ctx_in_debug}\n\n'

                    '-> Calling ctx._scope.cancel()!\n'
                )
                lock_cs.cancel()

            # TODO: wen do we actually want/need this, see above.
            # DebugStatus.unshield_sigint()
            log.warning(problem)

    # child actor that has locked the debugger
    elif not is_root_process():
        log.debug(
            f'Subactor {actor.uid} handling SIGINT\n\n'
            f'{Lock.repr()}\n'
        )

        rent_chan: Channel = actor._parent_chan
        if (
            rent_chan is None
            or
            not rent_chan.connected()
        ):
            log.warning(
                'This sub-actor thinks it is debugging '
                'but it has no connection to its parent ??\n'
                f'{actor.uid}\n'
                'Allowing SIGINT propagation..'
            )
            DebugStatus.unshield_sigint()

        repl_task: str|None = DebugStatus.repl_task
        req_task: str|None = DebugStatus.req_task
        if (
            repl_task
            and
            repl
        ):
            log.pdb(
                _ctlc_ignore_header
                +
                f' by local task\n\n'
                f'{repl_task}\n'
                f' |_{repl}\n'
            )
        elif req_task:
            log.debug(
                _ctlc_ignore_header
                +
                f' by local request-task and either,\n'
                f'- someone else is already REPL-in and has the `Lock`, or\n'
                f'- some other local task already is replin?\n\n'
                f'{req_task}\n'
            )

        # TODO can we remove this now?
        # -[ ] does this path ever get hit any more?
        else:
            msg: str = (
                'SIGINT shield handler still active BUT, \n\n'
            )
            if repl_task is None:
                msg += (
                    '- No local task claims to be in debug?\n'
                )

            if repl is None:
                msg += (
                    '- No local REPL is currently active?\n'
                )

            if req_task is None:
                msg += (
                    '- No debug request task is active?\n'
                )

            log.warning(
                msg
                +
                'Reverting handler to `trio` default!\n'
            )
            DebugStatus.unshield_sigint()

            # XXX ensure that the reverted-to-handler actually is
            # able to rx what should have been **this** KBI ;)
            do_cancel()

        # TODO: how to handle the case of an intermediary-child actor
        # that **is not** marked in debug mode? See oustanding issue:
        # https://github.com/goodboy/tractor/issues/320
        # elif debug_mode():

    # maybe redraw/print last REPL output to console since
    # we want to alert the user that more input is expect since
    # nothing has been done dur to ignoring sigint.
    if (
        DebugStatus.repl  # only when current actor has a REPL engaged
    ):
        flush_status: str = (
            'Flushing stdout to ensure new prompt line!\n'
        )

        # XXX: yah, mega hack, but how else do we catch this madness XD
        if (
            repl.shname == 'xonsh'
        ):
            flush_status += (
                '-> ALSO re-flushing due to `xonsh`..\n'
            )
            repl.stdout.write(repl.prompt)

        # log.warning(
        log.devx(
            flush_status
        )
        repl.stdout.flush()

        # TODO: better console UX to match the current "mode":
        # -[ ] for example if in sticky mode where if there is output
        #   detected as written to the tty we redraw this part underneath
        #   and erase the past draw of this same bit above?
        # repl.sticky = True
        # repl._print_if_sticky()

        # also see these links for an approach from `ptk`:
        # https://github.com/goodboy/tractor/issues/130#issuecomment-663752040
        # https://github.com/prompt-toolkit/python-prompt-toolkit/blob/c2c6af8a0308f9e5d7c0e28cb8a02963fe0ce07a/prompt_toolkit/patch_stdout.py
    else:
        log.devx(
        # log.warning(
            'Not flushing stdout since not needed?\n'
            f'|_{repl}\n'
        )

    # XXX only for tracing this handler
    log.devx('exiting SIGINT')
