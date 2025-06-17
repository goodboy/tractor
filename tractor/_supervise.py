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
``trio`` inspired apis and helpers

"""
from contextlib import asynccontextmanager as acm
from functools import partial
import inspect
from pprint import pformat
from typing import (
    TYPE_CHECKING,
)
import typing
import warnings

import trio


from .devx.debug import maybe_wait_for_debugger
from ._addr import (
    UnwrappedAddress,
    mk_uuid,
)
from ._state import current_actor, is_main_process
from .log import get_logger, get_loglevel
from ._runtime import Actor
from ._portal import Portal
from .trionics import (
    is_multi_cancelled,
    collapse_eg,
)
from ._exceptions import (
    ContextCancelled,
)
from ._root import (
    open_root_actor,
)
from . import _state
from . import _spawn


if TYPE_CHECKING:
    import multiprocessing as mp
    # from .ipc._server import IPCServer
    from .ipc import IPCServer


log = get_logger(__name__)


class ActorNursery:
    '''
    The fundamental actor supervision construct: spawn and manage
    explicit lifetime and capability restricted, bootstrapped,
    ``trio.run()`` scheduled sub-processes.

    Though the concept of a "process nursery" is different in complexity
    and slightly different in semantics then a tradtional single
    threaded task nursery, much of the interface is the same. New
    processes each require a top level "parent" or "root" task which is
    itself no different then any task started by a tradtional
    ``trio.Nursery``. The main difference is that each "actor" (a
    process + ``trio.run()``) contains a full, paralell executing
    ``trio``-task-tree. The following super powers ensue:

    - starting tasks in a child actor are completely independent of
      tasks started in the current process. They execute in *parallel*
      relative to tasks in the current process and are scheduled by their
      own actor's ``trio`` run loop.
    - tasks scheduled in a remote process still maintain an SC protocol
      across memory boundaries using a so called "structured concurrency
      dialogue protocol" which ensures task-hierarchy-lifetimes are linked.
    - remote tasks (in another actor) can fail and relay failure back to
      the caller task (in some other actor) via a seralized
      ``RemoteActorError`` which means no zombie process or RPC
      initiated task can ever go off on its own.

    '''
    def __init__(
        self,
        # TODO: maybe def these as fields of a struct looking type?
        actor: Actor,
        ria_nursery: trio.Nursery,
        da_nursery: trio.Nursery,
        errors: dict[tuple[str, str], BaseException],

    ) -> None:
        # self.supervisor = supervisor  # TODO
        self._actor: Actor = actor

        # TODO: rename to `._tn` for our conventional "task-nursery"
        self._da_nursery = da_nursery

        self._children: dict[
            tuple[str, str],
            tuple[
                Actor,
                trio.Process | mp.Process,
                Portal | None,
            ]
        ] = {}

        self.cancelled: bool = False
        self._join_procs = trio.Event()
        self._at_least_one_child_in_debug: bool = False
        self.errors = errors
        self._scope_error: BaseException|None = None
        self.exited = trio.Event()

        # NOTE: when no explicit call is made to
        # `.open_root_actor()` by application code,
        # `.open_nursery()` will implicitly call it to start the
        # actor-tree runtime. In this case we mark ourselves as
        # such so that runtime components can be aware for logging
        # and syncing purposes to any actor opened nurseries.
        self._implicit_runtime_started: bool = False

        # TODO: remove the `.run_in_actor()` API and thus this 2ndary
        # nursery when that API get's moved outside this primitive!
        self._ria_nursery = ria_nursery
        # portals spawned with ``run_in_actor()`` are
        # cancelled when their "main" result arrives
        self._cancel_after_result_on_exit: set = set()

    async def start_actor(
        self,
        name: str,

        *,

        bind_addrs: list[UnwrappedAddress]|None = None,
        rpc_module_paths: list[str]|None = None,
        enable_transports: list[str] = [_state._def_tpt_proto],
        enable_modules: list[str]|None = None,
        loglevel: str|None = None,  # set log level per subactor
        debug_mode: bool|None = None,
        infect_asyncio: bool = False,

        # TODO: ideally we can rm this once we no longer have
        # a `._ria_nursery` since the dependent APIs have been
        # removed!
        nursery: trio.Nursery|None = None,
        proc_kwargs: dict[str, any] = {}

    ) -> Portal:
        '''
        Start a (daemon) actor: an process that has no designated
        "main task" besides the runtime.

        '''
        __runtimeframe__: int = 1  # noqa
        loglevel: str = (
            loglevel
            or self._actor.loglevel
            or get_loglevel()
        )

        # configure and pass runtime state
        _rtv = _state._runtime_vars.copy()
        _rtv['_is_root'] = False
        _rtv['_is_infected_aio'] = infect_asyncio

        # allow setting debug policy per actor
        if debug_mode is not None:
            _rtv['_debug_mode'] = debug_mode
            self._at_least_one_child_in_debug = True

        enable_modules = enable_modules or []

        if rpc_module_paths:
            warnings.warn(
                "`rpc_module_paths` is now deprecated, use "
                " `enable_modules` instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            enable_modules.extend(rpc_module_paths)

        subactor = Actor(
            name=name,
            uuid=mk_uuid(),

            # modules allowed to invoked funcs from
            enable_modules=enable_modules,
            loglevel=loglevel,

            # verbatim relay this actor's registrar addresses
            registry_addrs=current_actor().reg_addrs,
        )
        parent_addr: UnwrappedAddress = self._actor.accept_addr
        assert parent_addr

        # start a task to spawn a process
        # blocks until process has been started and a portal setup
        nursery: trio.Nursery = nursery or self._da_nursery

        # XXX: the type ignore is actually due to a `mypy` bug
        return await nursery.start(  # type: ignore
            partial(
                _spawn.new_proc,
                name,
                self,
                subactor,
                self.errors,
                bind_addrs,
                parent_addr,
                _rtv,  # run time vars
                infect_asyncio=infect_asyncio,
                proc_kwargs=proc_kwargs
            )
        )

    # TODO: DEPRECATE THIS:
    # -[ ] impl instead as a hilevel wrapper on
    #   top of a `@context` style invocation.
    #  |_ dynamic @context decoration on child side
    #  |_ implicit `Portal.open_context() as (ctx, first):`
    #    and `return first` on parent side.
    #  |_ mention how it's similar to `trio-parallel` API?
    # -[ ] use @api_frame on the wrapper
    async def run_in_actor(
        self,

        fn: typing.Callable,
        *,

        name: str | None = None,
        bind_addrs: UnwrappedAddress|None = None,
        rpc_module_paths: list[str] | None = None,
        enable_modules: list[str] | None = None,
        loglevel: str | None = None,  # set log level per subactor
        infect_asyncio: bool = False,
        proc_kwargs: dict[str, any] = {},

        **kwargs,  # explicit args to ``fn``

    ) -> Portal:
        '''
        Spawn a new actor, run a lone task, then terminate the actor and
        return its result.

        Actors spawned using this method are kept alive at nursery teardown
        until the task spawned by executing ``fn`` completes at which point
        the actor is terminated.

        '''
        __runtimeframe__: int = 1  # noqa
        mod_path: str = fn.__module__

        if name is None:
            # use the explicit function name if not provided
            name = fn.__name__

        portal: Portal = await self.start_actor(
            name,
            enable_modules=[mod_path] + (
                enable_modules or rpc_module_paths or []
            ),
            bind_addrs=bind_addrs,
            loglevel=loglevel,
            # use the run_in_actor nursery
            nursery=self._ria_nursery,
            infect_asyncio=infect_asyncio,
            proc_kwargs=proc_kwargs
        )

        # XXX: don't allow stream funcs
        if not (
            inspect.iscoroutinefunction(fn) and
            not getattr(fn, '_tractor_stream_function', False)
        ):
            raise TypeError(f'{fn} must be an async function!')

        # this marks the actor to be cancelled after its portal result
        # is retreived, see logic in `open_nursery()` below.
        self._cancel_after_result_on_exit.add(portal)
        await portal._submit_for_result(
            mod_path,
            fn.__name__,
            **kwargs
        )
        return portal

    # @api_frame
    async def cancel(
        self,
        hard_kill: bool = False,

    ) -> None:
        '''
        Cancel this actor-nursery by instructing each subactor's
        runtime to cancel and wait for all underlying sub-processes
        to terminate.

        If `hard_kill` is set then kill the processes directly using
        the spawning-backend's API/OS-machinery without any attempt
        at (graceful) `trio`-style cancellation using our
        `Actor.cancel()`.

        '''
        __runtimeframe__: int = 1  # noqa
        self.cancelled = True

        # TODO: impl a repr for spawn more compact
        # then `._children`..
        children: dict = self._children
        child_count: int = len(children)
        msg: str = f'Cancelling actor nursery with {child_count} children\n'

        server: IPCServer = self._actor.ipc_server

        with trio.move_on_after(3) as cs:
            async with (
                collapse_eg(),
                trio.open_nursery() as tn,
            ):

                subactor: Actor
                proc: trio.Process
                portal: Portal
                for (
                    subactor,
                    proc,
                    portal,
                ) in children.values():

                    # TODO: are we ever even going to use this or
                    # is the spawning backend responsible for such
                    # things? I'm thinking latter.
                    if hard_kill:
                        proc.terminate()

                    else:
                        if portal is None:  # actor hasn't fully spawned yet
                            event: trio.Event = server._peer_connected[subactor.uid]
                            log.warning(
                                f"{subactor.uid} never 't finished spawning?"
                            )

                            await event.wait()

                            # channel/portal should now be up
                            _, _, portal = children[subactor.uid]

                            # XXX should be impossible to get here
                            # unless method was called from within
                            # shielded cancel scope.
                            if portal is None:
                                # cancelled while waiting on the event
                                # to arrive
                                chan = server._peers[subactor.uid][-1]
                                if chan:
                                    portal = Portal(chan)
                                else:  # there's no other choice left
                                    proc.terminate()

                        # spawn cancel tasks for each sub-actor
                        assert portal
                        if portal.channel.connected():
                            tn.start_soon(portal.cancel_actor)

                log.cancel(msg)
        # if we cancelled the cancel (we hung cancelling remote actors)
        # then hard kill all sub-processes
        if cs.cancelled_caught:
            log.error(
                f'Failed to cancel {self}?\n'
                'Hard killing underlying subprocess tree!\n'
            )
            subactor: Actor
            proc: trio.Process
            portal: Portal
            for (
                subactor,
                proc,
                portal,
            ) in children.values():
                log.warning(f"Hard killing process {proc}")
                proc.terminate()

        # mark ourselves as having (tried to have) cancelled all subactors
        self._join_procs.set()


@acm
async def _open_and_supervise_one_cancels_all_nursery(
    actor: Actor,
    tb_hide: bool = False,

) -> typing.AsyncGenerator[ActorNursery, None]:

    # normally don't need to show user by default
    __tracebackhide__: bool = tb_hide

    outer_err: BaseException|None = None
    inner_err: BaseException|None = None

    # the collection of errors retreived from spawned sub-actors
    errors: dict[tuple[str, str], BaseException] = {}

    # This is the outermost level "deamon actor" nursery. It is awaited
    # **after** the below inner "run in actor nursery". This allows for
    # handling errors that are generated by the inner nursery in
    # a supervisor strategy **before** blocking indefinitely to wait for
    # actors spawned in "daemon mode" (aka started using
    # `ActorNursery.start_actor()`).

    # errors from this daemon actor nursery bubble up to caller
    async with (
        collapse_eg(),
        trio.open_nursery() as da_nursery,
    ):
        try:
            # This is the inner level "run in actor" nursery. It is
            # awaited first since actors spawned in this way (using
            # `ActorNusery.run_in_actor()`) are expected to only
            # return a single result and then complete (i.e. be canclled
            # gracefully). Errors collected from these actors are
            # immediately raised for handling by a supervisor strategy.
            # As such if the strategy propagates any error(s) upwards
            # the above "daemon actor" nursery will be notified.
            async with (
                collapse_eg(),
                trio.open_nursery() as ria_nursery,
            ):
                an = ActorNursery(
                    actor,
                    ria_nursery,
                    da_nursery,
                    errors
                )
                try:
                    # spawning of actors happens in the caller's scope
                    # after we yield upwards
                    yield an

                    # When we didn't error in the caller's scope,
                    # signal all process-monitor-tasks to conduct
                    # the "hard join phase".
                    log.runtime(
                        'Waiting on subactors to complete:\n'
                        f'{pformat(an._children)}\n'
                    )
                    an._join_procs.set()

                except BaseException as _inner_err:
                    inner_err = _inner_err
                    errors[actor.uid] = inner_err

                    # If we error in the root but the debugger is
                    # engaged we don't want to prematurely kill (and
                    # thus clobber access to) the local tty since it
                    # will make the pdb repl unusable.
                    # Instead try to wait for pdb to be released before
                    # tearing down.
                    await maybe_wait_for_debugger(
                        child_in_debug=an._at_least_one_child_in_debug
                    )

                    # if the caller's scope errored then we activate our
                    # one-cancels-all supervisor strategy (don't
                    # worry more are coming).
                    an._join_procs.set()

                    # XXX NOTE XXX: hypothetically an error could
                    # be raised and then a cancel signal shows up
                    # slightly after in which case the `else:`
                    # block here might not complete?  For now,
                    # shield both.
                    with trio.CancelScope(shield=True):
                        etype: type = type(inner_err)
                        if etype in (
                            trio.Cancelled,
                            KeyboardInterrupt,
                        ) or (
                            is_multi_cancelled(inner_err)
                        ):
                            log.cancel(
                                f'Actor-nursery cancelled by {etype}\n\n'

                                f'{current_actor().uid}\n'
                                f' |_{an}\n\n'

                                # TODO: show tb str?
                                # f'{tb_str}'
                            )
                        elif etype in {
                            ContextCancelled,
                        }:
                            log.cancel(
                                'Actor-nursery caught remote cancellation\n'
                                '\n'
                                f'{inner_err.tb_str}'
                            )
                        else:
                            log.exception(
                                'Nursery errored with:\n'

                                # TODO: same thing as in
                                # `._invoke()` to compute how to
                                # place this div-line in the
                                # middle of the above msg
                                # content..
                                # -[ ] prolly helper-func it too
                                #   in our `.log` module..
                                # '------ - ------'
                            )

                        # cancel all subactors
                        await an.cancel()

            # ria_nursery scope end

        # TODO: this is the handler around the ``.run_in_actor()``
        # nursery. Ideally we can drop this entirely in the future as
        # the whole ``.run_in_actor()`` API should be built "on top of"
        # this lower level spawn-request-cancel "daemon actor" API where
        # a local in-actor task nursery is used with one-to-one task
        # + `await Portal.run()` calls and the results/errors are
        # handled directly (inline) and errors by the local nursery.
        except (
            Exception,
            BaseExceptionGroup,
            trio.Cancelled
        ) as _outer_err:
            outer_err = _outer_err

            an._scope_error = outer_err or inner_err

            # XXX: yet another guard before allowing the cancel
            # sequence in case a (single) child is in debug.
            await maybe_wait_for_debugger(
                child_in_debug=an._at_least_one_child_in_debug
            )

            # If actor-local error was raised while waiting on
            # ".run_in_actor()" actors then we also want to cancel all
            # remaining sub-actors (due to our lone strategy:
            # one-cancels-all).
            if an._children:
                log.cancel(
                    'Actor-nursery cancelling due error type:\n'
                    f'{outer_err}\n'
                )
                with trio.CancelScope(shield=True):
                    await an.cancel()
            raise

        finally:
            # No errors were raised while awaiting ".run_in_actor()"
            # actors but those actors may have returned remote errors as
            # results (meaning they errored remotely and have relayed
            # those errors back to this parent actor). The errors are
            # collected in ``errors`` so cancel all actors, summarize
            # all errors and re-raise.
            if errors:
                if an._children:
                    with trio.CancelScope(shield=True):
                        await an.cancel()

                # use `BaseExceptionGroup` as needed
                if len(errors) > 1:
                    raise BaseExceptionGroup(
                        'tractor.ActorNursery errored with',
                        tuple(errors.values()),
                    )
                else:
                    raise list(errors.values())[0]

            # show frame on any (likely) internal error
            if (
                not an.cancelled
                and an._scope_error
            ):
                __tracebackhide__: bool = False

        # da_nursery scope end - nursery checkpoint
    # final exit


# @api_frame
@acm
async def open_nursery(
    *,  # named params only!
    hide_tb: bool = True,
    **kwargs,
    # ^TODO, paramspec for `open_root_actor()`

) -> typing.AsyncGenerator[ActorNursery, None]:
    '''
    Create and yield a new ``ActorNursery`` to be used for spawning
    structured concurrent subactors.

    When an actor is spawned a new trio task is started which
    invokes one of the process spawning backends to create and start
    a new subprocess. These tasks are started by one of two nurseries
    detailed below. The reason for spawning processes from within
    a new task is because ``trio_run_in_process`` itself creates a new
    internal nursery and the same task that opens a nursery **must**
    close it. It turns out this approach is probably more correct
    anyway since it is more clear from the following nested nurseries
    which cancellation scopes correspond to each spawned subactor set.

    '''
    __tracebackhide__: bool = hide_tb
    implicit_runtime: bool = False
    actor: Actor = current_actor(err_on_no_runtime=False)
    an: ActorNursery|None = None
    try:
        if (
            actor is None
            and is_main_process()
        ):
            # if we are the parent process start the
            # actor runtime implicitly
            log.info("Starting actor runtime!")

            # mark us for teardown on exit
            implicit_runtime: bool = True

            async with open_root_actor(
                hide_tb=hide_tb,
                **kwargs,
            ) as actor:
                assert actor is current_actor()

                try:
                    async with _open_and_supervise_one_cancels_all_nursery(
                        actor
                    ) as an:

                        # NOTE: mark this nursery as having
                        # implicitly started the root actor so
                        # that `._runtime` machinery can avoid
                        # certain teardown synchronization
                        # blocking/waits and any associated (warn)
                        # logging when it's known that this
                        # nursery shouldn't be exited before the
                        # root actor is.
                        an._implicit_runtime_started = True
                        yield an
                finally:
                    # XXX: this event will be set after the root actor
                    # runtime is already torn down, so we want to
                    # avoid any blocking on it.
                    an.exited.set()

        else:  # sub-nursery case

            try:
                async with _open_and_supervise_one_cancels_all_nursery(
                    actor
                ) as an:
                    yield an
            finally:
                an.exited.set()

    finally:
        # show frame on any internal runtime-scope error
        if (
            an
            and
            not an.cancelled
            and
            an._scope_error
        ):
            __tracebackhide__: bool = False

        msg: str = (
            'Actor-nursery exited\n'
            f'|_{an}\n'
        )

        if implicit_runtime:
            # shutdown runtime if it was started and report noisly
            # that we're did so.
            msg += '=> Shutting down actor runtime <=\n'
            log.info(msg)

        else:
            # keep noise low during std operation.
            log.runtime(msg)
