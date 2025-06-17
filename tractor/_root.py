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
Root actor runtime ignition(s).

'''
from contextlib import (
    asynccontextmanager as acm,
)
from functools import partial
import importlib
import inspect
import logging
import os
import signal
import sys
from typing import (
    Any,
    Callable,
)
import warnings


import trio

from . import _runtime
from .devx import (
    debug,
    _frame_stack,
    pformat as _pformat,
)
from . import _spawn
from . import _state
from . import log
from .ipc import (
    _connect_chan,
)
from ._addr import (
    Address,
    UnwrappedAddress,
    default_lo_addrs,
    mk_uuid,
    wrap_address,
)
from .trionics import (
    is_multi_cancelled,
    collapse_eg,
)
from ._exceptions import (
    RuntimeFailure,
)


logger = log.get_logger('tractor')


# TODO: stick this in a `@acm` defined in `devx.debug`?
# -[ ] also maybe consider making this a `wrapt`-deco to
#     save an indent level?
#
@acm
async def maybe_block_bp(
    debug_mode: bool,
    maybe_enable_greenback: bool,
) -> bool:
    # Override the global debugger hook to make it play nice with
    # ``trio``, see much discussion in:
    # https://github.com/python-trio/trio/issues/1155#issuecomment-742964018
    builtin_bp_handler: Callable = sys.breakpointhook
    orig_bp_path: str|None = os.environ.get(
        'PYTHONBREAKPOINT',
        None,
    )
    bp_blocked: bool
    if (
        debug_mode
        and maybe_enable_greenback
        and (
            maybe_mod := await debug.maybe_init_greenback(
                raise_not_found=False,
            )
        )
    ):
        logger.info(
            f'Found `greenback` installed @ {maybe_mod}\n'
            'Enabling `tractor.pause_from_sync()` support!\n'
        )
        os.environ['PYTHONBREAKPOINT'] = (
            'tractor.devx.debug._sync_pause_from_builtin'
        )
        _state._runtime_vars['use_greenback'] = True
        bp_blocked = False

    else:
        # TODO: disable `breakpoint()` by default (without
        # `greenback`) since it will break any multi-actor
        # usage by a clobbered TTY's stdstreams!
        def block_bps(*args, **kwargs):
            raise RuntimeError(
                'Trying to use `breakpoint()` eh?\n\n'
                'Welp, `tractor` blocks `breakpoint()` built-in calls by default!\n'
                'If you need to use it please install `greenback` and set '
                '`debug_mode=True` when opening the runtime '
                '(either via `.open_nursery()` or `open_root_actor()`)\n'
            )

        sys.breakpointhook = block_bps
        # lol ok,
        # https://docs.python.org/3/library/sys.html#sys.breakpointhook
        os.environ['PYTHONBREAKPOINT'] = "0"
        bp_blocked = True

    try:
        yield bp_blocked
    finally:
        # restore any prior built-in `breakpoint()` hook state
        if builtin_bp_handler is not None:
            sys.breakpointhook = builtin_bp_handler

        if orig_bp_path is not None:
            os.environ['PYTHONBREAKPOINT'] = orig_bp_path

        else:
            # clear env back to having no entry
            os.environ.pop('PYTHONBREAKPOINT', None)



@acm
async def open_root_actor(
    *,
    # defaults are above
    registry_addrs: list[UnwrappedAddress]|None = None,

    # defaults are above
    arbiter_addr: tuple[UnwrappedAddress]|None = None,

    enable_transports: list[
        # TODO, this should eventually be the pairs as
        # defined by (codec, proto) as on `MsgTransport.
        _state.TransportProtocolKey,
    ]|None = None,

    name: str|None = 'root',

    # either the `multiprocessing` start method:
    # https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods
    # OR `trio` (the new default).
    start_method: _spawn.SpawnMethodKey|None = None,

    # enables the multi-process debugger support
    debug_mode: bool = False,
    maybe_enable_greenback: bool = False,  # `.pause_from_sync()/breakpoint()` support
    # ^XXX NOTE^ the perf implications of use,
    # https://greenback.readthedocs.io/en/latest/principle.html#performance
    enable_stack_on_sig: bool = False,

    # internal logging
    loglevel: str|None = None,

    enable_modules: list|None = None,
    rpc_module_paths: list|None = None,

    # NOTE: allow caller to ensure that only one registry exists
    # and that this call creates it.
    ensure_registry: bool = False,

    hide_tb: bool = True,

    # XXX, proxied directly to `.devx.debug._maybe_enter_pm()`
    # for REPL-entry logic.
    debug_filter: Callable[
        [BaseException|BaseExceptionGroup],
        bool,
    ] = lambda err: not is_multi_cancelled(err),

    # TODO, a way for actors to augment passing derived
    # read-only state to sublayers?
    # extra_rt_vars: dict|None = None,

) -> _runtime.Actor:
    '''
    Initialize the `tractor` runtime by starting a "root actor" in
    a parent-most Python process.

    All (disjoint) actor-process-trees-as-programs are created via
    this entrypoint.

    '''
    # XXX NEVER allow nested actor-trees!
    if already_actor := _state.current_actor(err_on_no_runtime=False):
        rtvs: dict[str, Any] = _state._runtime_vars
        root_mailbox: list[str, int] = rtvs['_root_mailbox']
        registry_addrs: list[list[str, int]] = rtvs['_registry_addrs']
        raise RuntimeFailure(
            f'A current actor already exists !?\n'
            f'({already_actor}\n'
            f'\n'
            f'You can NOT open a second root actor from within '
            f'an existing tree and the current root of this '
            f'already exists !!\n'
            f'\n'
            f'_root_mailbox: {root_mailbox!r}\n'
            f'_registry_addrs: {registry_addrs!r}\n'
        )

    async with maybe_block_bp(
        debug_mode=debug_mode,
        maybe_enable_greenback=maybe_enable_greenback,
    ):
        if enable_transports is None:
            enable_transports: list[str] = _state.current_ipc_protos()
        else:
            _state._runtime_vars['_enable_tpts'] = enable_transports

        # TODO! support multi-tpts per actor!
        # Bo
        if not len(enable_transports) == 1:
            raise RuntimeError(
                f'No multi-tpt support yet!\n'
                f'enable_transports={enable_transports!r}\n'
            )

        _frame_stack.hide_runtime_frames()
        __tracebackhide__: bool = hide_tb

        # attempt to retreive ``trio``'s sigint handler and stash it
        # on our debugger lock state.
        debug.DebugStatus._trio_handler = signal.getsignal(signal.SIGINT)

        # mark top most level process as root actor
        _state._runtime_vars['_is_root'] = True

        # caps based rpc list
        enable_modules = (
            enable_modules
            or
            []
        )

        if rpc_module_paths:
            warnings.warn(
                "`rpc_module_paths` is now deprecated, use "
                " `enable_modules` instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            enable_modules.extend(rpc_module_paths)

        if start_method is not None:
            _spawn.try_set_start_method(start_method)

        # TODO! remove this ASAP!
        if arbiter_addr is not None:
            warnings.warn(
                '`arbiter_addr` is now deprecated\n'
                'Use `registry_addrs: list[tuple]` instead..',
                DeprecationWarning,
                stacklevel=2,
            )
            registry_addrs = [arbiter_addr]

        if not registry_addrs:
            registry_addrs: list[UnwrappedAddress] = default_lo_addrs(
                enable_transports
            )

        assert registry_addrs

        loglevel = (
            loglevel
            or log._default_loglevel
        ).upper()

        if (
            debug_mode
            and
            _spawn._spawn_method == 'trio'
        ):
            _state._runtime_vars['_debug_mode'] = True

            # expose internal debug module to every actor allowing for
            # use of ``await tractor.pause()``
            enable_modules.append('tractor.devx.debug._tty_lock')

            # if debug mode get's enabled *at least* use that level of
            # logging for some informative console prompts.
            if (
                logging.getLevelName(
                    # lul, need the upper case for the -> int map?
                    # sweet "dynamic function behaviour" stdlib...
                    loglevel,
                ) > logging.getLevelName('PDB')
            ):
                loglevel = 'PDB'


        elif debug_mode:
            raise RuntimeError(
                "Debug mode is only supported for the `trio` backend!"
            )

        assert loglevel
        _log = log.get_console_log(loglevel)
        assert _log

        # TODO: factor this into `.devx._stackscope`!!
        if (
            debug_mode
            and
            enable_stack_on_sig
        ):
            from .devx._stackscope import enable_stack_on_sig
            enable_stack_on_sig()

        # closed into below ping task-func
        ponged_addrs: list[UnwrappedAddress] = []

        async def ping_tpt_socket(
            addr: UnwrappedAddress,
            timeout: float = 1,
        ) -> None:
            '''
            Attempt temporary connection to see if a registry is
            listening at the requested address by a tranport layer
            ping.

            If a connection can't be made quickly we assume none no
            server is listening at that addr.

            '''
            try:
                # TODO: this connect-and-bail forces us to have to
                # carefully rewrap TCP 104-connection-reset errors as
                # EOF so as to avoid propagating cancel-causing errors
                # to the channel-msg loop machinery. Likely it would
                # be better to eventually have a "discovery" protocol
                # with basic handshake instead?
                with trio.move_on_after(timeout):
                    async with _connect_chan(addr):
                        ponged_addrs.append(addr)

            except OSError:
                # TODO: make this a "discovery" log level?
                logger.info(
                    f'No actor registry found @ {addr}\n'
                )

        async with trio.open_nursery() as tn:
            for addr in registry_addrs:
                tn.start_soon(
                    ping_tpt_socket,
                    addr,
                )

        trans_bind_addrs: list[UnwrappedAddress] = []

        # Create a new local root-actor instance which IS NOT THE
        # REGISTRAR
        if ponged_addrs:
            if ensure_registry:
                raise RuntimeError(
                     f'Failed to open `{name}`@{ponged_addrs}: '
                    'registry socket(s) already bound'
                )

            # we were able to connect to an arbiter
            logger.info(
                f'Registry(s) seem(s) to exist @ {ponged_addrs}'
            )

            actor = _runtime.Actor(
                name=name or 'anonymous',
                uuid=mk_uuid(),
                registry_addrs=ponged_addrs,
                loglevel=loglevel,
                enable_modules=enable_modules,
            )
            # DO NOT use the registry_addrs as the transport server
            # addrs for this new non-registar, root-actor.
            for addr in ponged_addrs:
                waddr: Address = wrap_address(addr)
                trans_bind_addrs.append(
                    waddr.get_random(bindspace=waddr.bindspace)
                )

        # Start this local actor as the "registrar", aka a regular
        # actor who manages the local registry of "mailboxes" of
        # other process-tree-local sub-actors.
        else:

            # NOTE that if the current actor IS THE REGISTAR, the
            # following init steps are taken:
            # - the tranport layer server is bound to each addr
            #   pair defined in provided registry_addrs, or the default.
            trans_bind_addrs = registry_addrs

            # - it is normally desirable for any registrar to stay up
            #   indefinitely until either all registered (child/sub)
            #   actors are terminated (via SC supervision) or,
            #   a re-election process has taken place.
            # NOTE: all of ^ which is not implemented yet - see:
            # https://github.com/goodboy/tractor/issues/216
            # https://github.com/goodboy/tractor/pull/348
            # https://github.com/goodboy/tractor/issues/296

            # TODO: rename as `RootActor` or is that even necessary?
            actor = _runtime.Arbiter(
                name=name or 'registrar',
                uuid=mk_uuid(),
                registry_addrs=registry_addrs,
                loglevel=loglevel,
                enable_modules=enable_modules,
            )
            # XXX, in case the root actor runtime was actually run from
            # `tractor.to_asyncio.run_as_asyncio_guest()` and NOt
            # `.trio.run()`.
            actor._infected_aio = _state._runtime_vars['_is_infected_aio']

        # Start up main task set via core actor-runtime nurseries.
        try:
            # assign process-local actor
            _state._current_actor = actor

            # start local channel-server and fake the portal API
            # NOTE: this won't block since we provide the nursery
            ml_addrs_str: str = '\n'.join(
                f'@{addr}' for addr in trans_bind_addrs
            )
            logger.info(
                f'Starting local {actor.uid} on the following transport addrs:\n'
                f'{ml_addrs_str}'
            )

            # start runtime in a bg sub-task, yield to caller.
            async with (
                collapse_eg(),
                trio.open_nursery() as root_tn,
            ):

                # `_runtime.async_main()` creates an internal nursery
                # and blocks here until any underlying actor(-process)
                # tree has terminated thereby conducting so called
                # "end-to-end" structured concurrency throughout an
                # entire hierarchical python sub-process set; all
                # "actor runtime" primitives are SC-compat and thus all
                # transitively spawned actors/processes must be as
                # well.
                await root_tn.start(
                    partial(
                        _runtime.async_main,
                        actor,
                        accept_addrs=trans_bind_addrs,
                        parent_addr=None
                    )
                )
                try:
                    yield actor
                except (
                    Exception,
                    BaseExceptionGroup,
                ) as err:

                    # TODO, in beginning to handle the subsubactor with
                    # crashed grandparent cases..
                    #
                    # was_locked: bool = await debug.maybe_wait_for_debugger(
                    #     child_in_debug=True,
                    # )
                    # XXX NOTE XXX see equiv note inside
                    # `._runtime.Actor._stream_handler()` where in the
                    # non-root or root-that-opened-this-mahually case we
                    # wait for the local actor-nursery to exit before
                    # exiting the transport channel handler.
                    entered: bool = await debug._maybe_enter_pm(
                        err,
                        api_frame=inspect.currentframe(),
                        debug_filter=debug_filter,
                    )

                    if (
                        not entered
                        and
                        not is_multi_cancelled(
                            err,
                        )
                    ):
                        logger.exception(
                            'Root actor crashed\n'
                            f'>x)\n'
                            f' |_{actor}\n'
                        )

                    # ALWAYS re-raise any error bubbled up from the
                    # runtime!
                    raise

                finally:
                    # NOTE: not sure if we'll ever need this but it's
                    # possibly better for even more determinism?
                    # logger.cancel(
                    #     f'Waiting on {len(nurseries)} nurseries in root..')
                    # nurseries = actor._actoruid2nursery.values()
                    # async with trio.open_nursery() as tempn:
                    #     for an in nurseries:
                    #         tempn.start_soon(an.exited.wait)

                    op_nested_actor_repr: str = _pformat.nest_from_op(
                        input_op='>) ',
                        tree_str=actor.pformat(),
                        nest_prefix='|_',
                    )
                    logger.info(
                        f'Closing down root actor\n'
                        f'{op_nested_actor_repr}\n'
                    )
                    await actor.cancel(None)  # self cancel
        finally:
            # revert all process-global runtime state
            if (
                debug_mode
                and
                _spawn._spawn_method == 'trio'
            ):
                _state._runtime_vars['_debug_mode'] = False

            _state._current_actor = None
            _state._last_actor_terminated = actor

            logger.runtime(
                f'Root actor terminated\n'
                f')>\n'
                f' |_{actor}\n'
            )


def run_daemon(
    enable_modules: list[str],

    # runtime kwargs
    name: str | None = 'root',
    registry_addrs: list[UnwrappedAddress]|None = None,

    start_method: str | None = None,
    debug_mode: bool = False,

    # TODO, support `infected_aio=True` mode by,
    # - calling the appropriate entrypoint-func from `.to_asyncio`
    # - maybe init-ing `greenback` as done above in
    #   `open_root_actor()`.

    **kwargs

) -> None:
    '''
    Spawn a root (daemon) actor which will respond to RPC; the main
    task simply starts the runtime and then blocks via embedded
    `trio.sleep_forever()`.

    This is a very minimal convenience wrapper around starting
    a "run-until-cancelled" root actor which can be started with a set
    of enabled modules for RPC request handling.

    '''
    kwargs['enable_modules'] = list(enable_modules)

    for path in enable_modules:
        importlib.import_module(path)

    async def _main():
        async with open_root_actor(
            registry_addrs=registry_addrs,
            name=name,
            start_method=start_method,
            debug_mode=debug_mode,
            **kwargs,
        ):
            return await trio.sleep_forever()

    return trio.run(_main)
