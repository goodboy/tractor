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
The `trio`-subprocess backend; the default for cross-platform.

Spawns sub-actors as fresh OS processes driven by
`trio.lowlevel.open_process()`.

'''
from __future__ import annotations
import sys
from typing import (
    Any,
    TYPE_CHECKING,
)

import trio
from trio import TaskStatus

from ..devx import (
    debug,
    pformat as _pformat,
)
from tractor.runtime._state import (
    current_actor,
    is_root_process,
    debug_mode,
)
from tractor.log import get_logger
from tractor.discovery._addr import UnwrappedAddress
from tractor.runtime._portal import Portal
from tractor.runtime._runtime import Actor
from tractor.msg import (
    types as msgtypes,
    pretty_struct,
)
from ._spawn import (
    hard_kill,
    soft_kill,
    wait_for_peer_or_proc_death,
)


if TYPE_CHECKING:
    from tractor.ipc import (
        _server,
    )
    from tractor.runtime._supervise import ActorNursery


log = get_logger('tractor')


async def trio_proc(
    name: str,
    actor_nursery: ActorNursery,
    subactor: Actor,
    errors: dict[tuple[str, str], Exception],

    # passed through to actor main
    bind_addrs: list[UnwrappedAddress],
    parent_addr: UnwrappedAddress,
    _runtime_vars: dict[str, Any],  # serialized and sent to _child
    *,
    infect_asyncio: bool = False,
    task_status: TaskStatus[Portal] = trio.TASK_STATUS_IGNORED,
    proc_kwargs: dict[str, any] = {}

) -> None:
    '''
    Create a new ``Process`` using a "spawn method" as (configured
    using ``try_set_start_method()``).

    This routine should be started in a actor runtime task and the
    logic here is to be considered the core supervision strategy.

    '''
    spawn_cmd = [
        sys.executable,
        "-m",
        # Hardcode this (instead of using ``_child.__name__`` to
        # avoid a double import warning:
        # https://stackoverflow.com/a/45070583
        "tractor._child",
        # We provide the child's unique identifier on this exec/spawn
        # line for debugging purposes when viewing the process tree
        # from the OS; it otherwise can be passed via the parent
        # channel if we prefer in the future (for privacy).
        "--uid",
        # TODO, how to pass this over "wire" encodings like
        # cmdline args?
        # -[ ] maybe we can add an `msgtypes.Aid.min_tuple()` ?
        str(subactor.aid.uid),
        # Address the child must connect to on startup
        "--parent_addr",
        str(parent_addr)
    ]

    if subactor.loglevel:
        spawn_cmd += [
            "--loglevel",
            subactor.loglevel
        ]
    # Tell child to run in guest mode on top of ``asyncio`` loop
    if infect_asyncio:
        spawn_cmd.append('--asyncio')

    cancelled_during_spawn: bool = False
    proc: trio.Process|None = None
    ipc_server: _server.Server = actor_nursery._actor.ipc_server
    peer_event: trio.Event|None = None
    try:
        try:
            proc: trio.Process = await trio.lowlevel.open_process(
                spawn_cmd,
                **proc_kwargs,
            )
            log.runtime(
                f'Started new child subproc\n'
                f'(>\n'
                f' |_{proc}\n'
            )

            # `ActorNursery.cancel()` may inspect this event as soon
            # as the provisional child is published below. Register
            # the event synchronously before
            # `wait_for_peer_or_proc_death()` opens its nursery and
            # checkpoints.
            peer_event = ipc_server._peer_connected.setdefault(
                subactor.aid.uid,
                trio.Event(),
            )

            # No `Portal` exists until the IPC handshake returns
            # `chan`. Replace this provisional entry with
            # `Portal(chan)` below.
            (
                reap_request,
                _,
                cancel_during_registration,
            ) = actor_nursery._register_child(
                subactor=subactor,
                proc=proc,
                portal=None,
            )
            if cancel_during_registration:
                cancelled_during_spawn = True
                proc.kill()
                raise RuntimeError(
                    'Actor registered after its nursery began '
                    'cancelling'
                )

            # wait for actor to spawn and connect back to us
            # channel should have handshake completed by the
            # local actor by the time we get a ref to it
            event, chan = await wait_for_peer_or_proc_death(
                ipc_server=ipc_server,
                uid=subactor.aid.uid,
                proc_wait=proc.wait,
                proc_repr=proc,
            )

        except trio.Cancelled:
            cancelled_during_spawn = True
            # we may cancel before the child connects back in which
            # case avoid clobbering the pdb tty.
            if debug_mode():
                with trio.CancelScope(shield=True):
                    # don't clobber an ongoing pdb
                    if is_root_process():
                        await debug.maybe_wait_for_debugger()

                    elif proc is not None:
                        async with debug.acquire_debug_lock(
                            subactor_uid=subactor.aid.uid
                        ):
                            # soft wait on the proc to terminate
                            with trio.move_on_after(0.5):
                                await proc.wait()
            raise

        # a sub-proc ref **must** exist now
        assert proc

        portal = Portal(chan)
        actor_nursery._children[subactor.aid.uid] = (
            subactor,
            proc,
            portal,
        )

        # send a "spawning specification" which configures the
        # initial runtime state of the child.
        sspec = msgtypes.SpawnSpec(
            _parent_main_data=subactor._parent_main_data,
            enable_modules=subactor.enable_modules,
            reg_addrs=subactor.reg_addrs,
            bind_addrs=bind_addrs,
            _runtime_vars=_runtime_vars,
        )
        log.runtime(
            f'Sending spawn spec to child\n'
            f'{{}}=> {chan.aid.reprol()!r}\n'
            f'\n'
            f'{pretty_struct.pformat(sspec)}\n'
        )
        await chan.send(sspec)

        # track subactor in current nursery
        curr_actor: Actor = current_actor()
        curr_actor._actoruid2nursery[subactor.aid.uid] = actor_nursery

        # resume caller at next checkpoint now that child is up
        task_status.started(portal)

        # wait for this child or its `ActorNursery` to request
        # process joining.
        with trio.CancelScope(shield=True):
            await reap_request.wait()

        # This is a "soft" (cancellable) join/reap which
        # will remote cancel the actor on a ``trio.Cancelled``
        # condition.
        await soft_kill(
            proc,
            trio.Process.wait,  # XXX, uses `pidfd_open()` below.
            portal
        )

    finally:
        # XXX NOTE XXX: The "hard" reap since no actor zombies are
        # allowed! Do this **after** cancellation/teardown to avoid
        # killing the process too early.
        if proc:
            reap_repr: str = _pformat.nest_from_op(
                input_op='>x)',
                text=subactor.pformat(),
            )
            log.cancel(
                f'Hard reap sequence starting for subactor\n'
                f'{reap_repr}'
            )

            with trio.CancelScope(shield=True):
                # don't clobber an ongoing pdb
                if cancelled_during_spawn:
                    # Try again to avoid TTY clobbering.
                    async with debug.acquire_debug_lock(
                        subactor_uid=subactor.aid.uid
                    ):
                        with trio.move_on_after(0.5):
                            await proc.wait()

                await debug.maybe_wait_for_debugger(
                    # NOTE: use the child's `_runtime_vars`
                    # (the fn-arg dict shipped via `SpawnSpec`)
                    # — NOT `get_runtime_vars()` which returns
                    # the *parent's* live runtime state.
                    child_in_debug=_runtime_vars.get(
                        '_debug_mode', False
                    ),
                    header_msg=(
                        'Delaying subproc reaper while debugger locked..\n'
                    ),

                    # TODO: need a diff value then default?
                    # poll_steps=9999999,
                )
                # TODO: solve the following issue where we need
                # to do a similar wait like this but in an
                # "intermediary" parent actor that itself isn't
                # in debug but has a child that is, and we need
                # to hold off on relaying SIGINT until that child
                # is complete.
                # https://github.com/goodboy/tractor/issues/320
                # -[ ] we need to handle non-root parent-actors
                # specially by somehow determining if a child is in
                # debug and then avoiding cancel/kill of said child
                # by this (intermediary) parent until such a time as
                # the root says the pdb lock is released and we are
                # good to tear down (our children)..
                #
                # -[ ] so maybe something like this where we try to
                #     acquire the lock and get notified of who has
                #     it, check that uid against our known children?
                # this_uid: tuple[str, str] = current_actor().uid
                # await debug.acquire_debug_lock(this_uid)

                if proc.poll() is None:
                    log.cancel(f'Attempting to hard kill {proc}')
                    await hard_kill(
                        proc,
                        # NOTE, pass through so post-SIGKILL we
                        # can `os.unlink()` the subactor's
                        # orphaned UDS sock-file(s) — the
                        # subactor's own
                        # `_serve_ipc_eps`-`finally:` cleanup
                        # never runs under SIGKILL. `subactor`
                        # lets the helper reconstruct the
                        # sock path via `aid.name + proc.pid`
                        # when `bind_addrs` is the common
                        # self-assigned-random case
                        # (bind_addrs=None at spawn). See
                        # `_unlink_uds_bind_addrs()` in `_spawn`.
                        bind_addrs=bind_addrs,
                        subactor=subactor,
                    )

                log.debug(f'Joined {proc}')
        else:
            log.warning('Nursery cancelled before sub-proc started')

        if (
            peer_event is not None
            and
            ipc_server._peer_connected.get(
                subactor.aid.uid,
            ) is peer_event
        ):
            ipc_server._peer_connected.pop(subactor.aid.uid)

        if not cancelled_during_spawn:
            # pop child entry to indicate we no longer managing this
            # subactor
            actor_nursery._children.pop(subactor.aid.uid)
