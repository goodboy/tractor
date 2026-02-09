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
Post-mortem debugging APIs and surrounding machinery for both
sync and async contexts.

Generally we maintain the same semantics a `pdb.post.mortem()` but
with actor-tree-wide sync/cooperation around any (sub)actor's use of
the root's TTY.

'''
from __future__ import annotations
import bdb
from contextlib import (
    AbstractContextManager,
    contextmanager as cm,
    nullcontext,
)
from functools import (
    partial,
)
import inspect
import sys
import traceback
from typing import (
    Callable,
    Sequence,
    Type,
    TYPE_CHECKING,
)
from types import (
    TracebackType,
    FrameType,
)

from msgspec import Struct
import trio
from tractor._exceptions import (
    NoRuntime,
)
from tractor import _state
from tractor._state import (
    current_actor,
    debug_mode,
)
from tractor.log import get_logger
from tractor.trionics import (
    is_multi_cancelled,
)
from ._trace import (
    _pause,
)
from ._tty_lock import (
    DebugStatus,
)
from ._repl import (
    PdbREPL,
    mk_pdb,
    TractorConfig as TractorConfig,
)

if TYPE_CHECKING:
    from trio.lowlevel import Task
    from tractor._runtime import (
        Actor,
    )

_crash_msg: str = (
    'Opening a pdb REPL in crashed actor'
)

log = get_logger()


class BoxedMaybeException(Struct):
    '''
    Box a maybe-exception for post-crash introspection usage
    from the body of a `open_crash_handler()` scope.

    '''
    value: BaseException|None = None

    # handler can suppress crashes dynamically
    raise_on_exit: bool|Sequence[Type[BaseException]] = True

    def pformat(self) -> str:
        '''
        Repr the boxed `.value` error in more-than-string
        repr form.

        '''
        if not self.value:
            return f'<{type(self).__name__}( .value=None )>'

        return (
            f'<{type(self.value).__name__}(\n'
            f' |_.value = {self.value}\n'
            f')>\n'
        )

    __repr__ = pformat


def _post_mortem(
    repl: PdbREPL,  # normally passed by `_pause()`

    # XXX all `partial`-ed in by `post_mortem()` below!
    tb: TracebackType,
    api_frame: FrameType,

    shield: bool = False,
    hide_tb: bool = True,

    # maybe pre/post REPL entry
    repl_fixture: (
        AbstractContextManager[bool]
        |None
    ) = None,

    boxed_maybe_exc: BoxedMaybeException|None = None,

) -> None:
    '''
    Enter the ``pdbpp`` port mortem entrypoint using our custom
    debugger instance.

    '''
    __tracebackhide__: bool = hide_tb

    # maybe enter any user fixture
    enter_repl: bool = DebugStatus.maybe_enter_repl_fixture(
        repl=repl,
        repl_fixture=repl_fixture,
        boxed_maybe_exc=boxed_maybe_exc,
    )
    try:
        if not enter_repl:
            # XXX, trigger `.release()` below immediately!
            return
        try:
            actor: Actor = current_actor()
            actor_repr: str = str(actor.uid)
            # ^TODO, instead a nice runtime-info + maddr + uid?
            # -[ ] impl a `Actor.__repr()__`??
            #  |_ <task>:<thread> @ <actor>

        except NoRuntime:
            actor_repr: str = '<no-actor-runtime?>'

        try:
            task_repr: Task = trio.lowlevel.current_task()
        except RuntimeError:
            task_repr: str = '<unknown-Task>'

        # TODO: print the actor supervion tree up to the root
        # here! Bo
        log.pdb(
            f'{_crash_msg}\n'
            f'x>(\n'
            f' |_ {task_repr} @ {actor_repr}\n'

        )

        # XXX NOTE(s) on `pdbp.xpm()` version..
        #
        # - seems to lose the up-stack tb-info?
        # - currently we're (only) replacing this from `pdbp.xpm()`
        #   to add the `end=''` to the print XD
        #
        print(traceback.format_exc(), end='')
        caller_frame: FrameType = api_frame.f_back

        # NOTE, see the impl details of these in the lib to
        # understand usage:
        # - `pdbp.post_mortem()`
        # - `pdbp.xps()`
        # - `bdb.interaction()`
        repl.reset()
        repl.interaction(
            frame=caller_frame,
            # frame=None,
            traceback=tb,
        )
    finally:
        # XXX NOTE XXX: this is abs required to avoid hangs!
        #
        # Since we presume the post-mortem was enaged to
        # a task-ending error, we MUST release the local REPL request
        # so that not other local task nor the root remains blocked!
        DebugStatus.release()


async def post_mortem(
    *,
    tb: TracebackType|None = None,
    api_frame: FrameType|None = None,
    hide_tb: bool = False,

    # TODO: support shield here just like in `pause()`?
    # shield: bool = False,

    **_pause_kwargs,

) -> None:
    '''
    Our builtin async equivalient of `pdb.post_mortem()` which can be
    used inside exception handlers.

    It's also used for the crash handler when `debug_mode == True` ;)

    '''
    __tracebackhide__: bool = hide_tb

    tb: TracebackType = tb or sys.exc_info()[2]

    # TODO: do upward stack scan for highest @api_frame and
    # use its parent frame as the expected user-app code
    # interact point.
    api_frame: FrameType = api_frame or inspect.currentframe()

    # TODO, move to submod `._pausing` or ._api? _trace
    await _pause(
        debug_func=partial(
            _post_mortem,
            api_frame=api_frame,
            tb=tb,
        ),
        hide_tb=hide_tb,
        **_pause_kwargs
    )


async def _maybe_enter_pm(
    err: BaseException,
    *,
    tb: TracebackType|None = None,
    api_frame: FrameType|None = None,
    hide_tb: bool = True,

    # only enter debugger REPL when returns `True`
    debug_filter: Callable[
        [BaseException|BaseExceptionGroup],
        bool,
    ] = lambda err: not is_multi_cancelled(err),
    **_pause_kws,
):
    if (
        debug_mode()

        # NOTE: don't enter debug mode recursively after quitting pdb
        # Iow, don't re-enter the repl if the `quit` command was issued
        # by the user.
        and not isinstance(err, bdb.BdbQuit)

        # XXX: if the error is the likely result of runtime-wide
        # cancellation, we don't want to enter the debugger since
        # there's races between when the parent actor has killed all
        # comms and when the child tries to contact said parent to
        # acquire the tty lock.

        # Really we just want to mostly avoid catching KBIs here so there
        # might be a simpler check we can do?
        and
        debug_filter(err)
    ):
        api_frame: FrameType = api_frame or inspect.currentframe()
        tb: TracebackType = tb or sys.exc_info()[2]
        await post_mortem(
            api_frame=api_frame,
            tb=tb,
            **_pause_kws,
        )
        return True

    else:
        return False


# TODO: better naming and what additionals?
# - [ ] optional runtime plugging?
# - [ ] detection for sync vs. async code?
# - [ ] specialized REPL entry when in distributed mode?
# -[x] hide tb by def
# - [x] allow ignoring kbi Bo
@cm
def open_crash_handler(
    catch: set[BaseException] = {
        BaseException,
    },
    ignore: set[BaseException] = {
        KeyboardInterrupt,
        trio.Cancelled,
    },
    hide_tb: bool = True,

    repl_fixture: (
        AbstractContextManager[bool]  # pre/post REPL entry
        |None
    ) = None,
    raise_on_exit: bool|Sequence[Type[BaseException]] = True,
):
    '''
    Generic "post mortem" crash handler using `pdbp` REPL debugger.

    We expose this as a CLI framework addon to both `click` and
    `typer` users so they can quickly wrap cmd endpoints which get
    automatically wrapped to use the runtime's `debug_mode: bool`
    AND `pdbp.pm()` around any code that is PRE-runtime entry
    - any sync code which runs BEFORE the main call to
      `trio.run()`.

    '''
    __tracebackhide__: bool = hide_tb

    # TODO, yield a `outcome.Error`-like boxed type?
    # -[~] use `outcome.Value/Error` X-> frozen!
    # -[x] write our own..?
    # -[ ] consider just wtv is used by `pytest.raises()`?
    #
    boxed_maybe_exc = BoxedMaybeException(
        raise_on_exit=raise_on_exit,
    )
    err: BaseException
    try:
        yield boxed_maybe_exc
    except tuple(catch) as err:
        boxed_maybe_exc.value = err
        if (
            type(err) not in ignore
            and
            not is_multi_cancelled(
                err,
                ignore_nested=ignore
            )
        ):
            try:
                # use our re-impl-ed version of `pdbp.xpm()`
                _post_mortem(
                    repl=mk_pdb(),
                    tb=sys.exc_info()[2],
                    api_frame=inspect.currentframe().f_back,
                    hide_tb=hide_tb,

                    repl_fixture=repl_fixture,
                    boxed_maybe_exc=boxed_maybe_exc,
                )
            except bdb.BdbQuit:
                __tracebackhide__: bool = False
                raise err

        if (
            raise_on_exit is True
            or (
                raise_on_exit is not False
                and (
                    set(raise_on_exit)
                    and
                    type(err) in raise_on_exit
                )
            )
            and
            boxed_maybe_exc.raise_on_exit == raise_on_exit
        ):
            raise err


@cm
def maybe_open_crash_handler(
    pdb: bool|None = None,
    hide_tb: bool = True,

    **kwargs,
):
    '''
    Same as `open_crash_handler()` but with bool input flag
    to allow conditional handling.

    Normally this is used with CLI endpoints such that if the --pdb
    flag is passed the pdb REPL is engaed on any crashes B)

    '''
    __tracebackhide__: bool = hide_tb

    if pdb is None:
        pdb: bool = _state.is_debug_mode()

    rtctx = nullcontext(
        enter_result=BoxedMaybeException()
    )
    if pdb:
        rtctx = open_crash_handler(
            hide_tb=hide_tb,
            **kwargs,
        )

    with rtctx as boxed_maybe_exc:
        yield boxed_maybe_exc
