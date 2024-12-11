# tractor: structured concurrent "actors".
# Copyright 2024-eternity Tyler Goodlet.

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
Daemon subactor as service(s) management and supervision primitives
and API.

'''
from __future__ import annotations
from contextlib import (
    asynccontextmanager as acm,
    # contextmanager as cm,
)
from collections import defaultdict
from dataclasses import (
    dataclass,
    field,
)
import functools
import inspect
from typing import (
    Callable,
    Any,
)

import tractor
import trio
from trio import TaskStatus
from tractor import (
    log,
    ActorNursery,
    current_actor,
    ContextCancelled,
    Context,
    Portal,
)

log = log.get_logger('tractor')


# TODO: implement a `@singleton` deco-API for wrapping the below
# factory's impl for general actor-singleton use?
#
# -[ ] go through the options peeps on SO did?
#  * https://stackoverflow.com/questions/6760685/what-is-the-best-way-of-implementing-singleton-in-python
#  * including @mikenerone's answer
#   |_https://stackoverflow.com/questions/6760685/what-is-the-best-way-of-implementing-singleton-in-python/39186313#39186313
#
# -[ ] put it in `tractor.lowlevel._globals` ?
#  * fits with our oustanding actor-local/global feat req?
#   |_ https://github.com/goodboy/tractor/issues/55
#  * how can it relate to the `Actor.lifetime_stack` that was
#    silently patched in?
#   |_ we could implicitly call both of these in the same
#     spot in the runtime using the lifetime stack?
#    - `open_singleton_cm().__exit__()`
#    -`del_singleton()`
#   |_ gives SC fixtue semantics to sync code oriented around
#     sub-process lifetime?
#  * what about with `trio.RunVar`?
#   |_https://trio.readthedocs.io/en/stable/reference-lowlevel.html#trio.lowlevel.RunVar
#    - which we'll need for no-GIL cpython (right?) presuming
#      multiple `trio.run()` calls in process?
#
#
# @singleton
# async def open_service_mngr(
#     **init_kwargs,
# ) -> ServiceMngr:
#     '''
#     Note this function body is invoke IFF no existing singleton instance already
#     exists in this proc's memory.

#     '''
#     # setup
#     yield ServiceMngr(**init_kwargs)
#     # teardown


# a deletion API for explicit instance de-allocation?
# @open_service_mngr.deleter
# def del_service_mngr() -> None:
#     mngr = open_service_mngr._singleton[0]
#     open_service_mngr._singleton[0] = None
#     del mngr



# TODO: implement a singleton deco-API for wrapping the below
# factory's impl for general actor-singleton use?
#
# @singleton
# async def open_service_mngr(
#     **init_kwargs,
# ) -> ServiceMngr:
#     '''
#     Note this function body is invoke IFF no existing singleton instance already
#     exists in this proc's memory.

#     '''
#     # setup
#     yield ServiceMngr(**init_kwargs)
#     # teardown



# TODO: singleton factory API instead of a class API
@acm
async def open_service_mngr(
    *,
    debug_mode: bool = False,

    # NOTE; since default values for keyword-args are effectively
    # module-vars/globals as per the note from,
    # https://docs.python.org/3/tutorial/controlflow.html#default-argument-values
    #
    # > "The default value is evaluated only once. This makes
    #   a difference when the default is a mutable object such as
    #   a list, dictionary, or instances of most classes"
    #
    _singleton: list[ServiceMngr|None] = [None],
    **init_kwargs,

) -> ServiceMngr:
    '''
    Open an actor-global "service-manager" for supervising a tree
    of subactors and/or actor-global tasks.

    The delivered `ServiceMngr` is singleton instance for each
    actor-process, that is, allocated on first open and never
    de-allocated unless explicitly deleted by al call to
    `del_service_mngr()`.

    '''
    # TODO: factor this an allocation into
    # a `._mngr.open_service_mngr()` and put in the
    # once-n-only-once setup/`.__aenter__()` part!
    # -[ ] how to make this only happen on the `mngr == None` case?
    #  |_ use `.trionics.maybe_open_context()` (for generic
    #     async-with-style-only-once of the factory impl, though
    #     what do we do for the allocation case?
    #    / `.maybe_open_nursery()` (since for this specific case
    #    it's simpler?) to activate
    async with (
        tractor.open_nursery() as an,
        trio.open_nursery() as tn,
    ):
        # impl specific obvi..
        init_kwargs.update({
            'actor_n': an,
            'service_n': tn,
        })

        mngr: ServiceMngr|None
        if (mngr := _singleton[0]) is None:

            log.info('Allocating a new service mngr!')
            mngr = _singleton[0] = ServiceMngr(**init_kwargs)

            # TODO: put into `.__aenter__()` section of
            # eventual `@singleton_acm` API wrapper.
            #
            # assign globally for future daemon/task creation
            mngr.actor_n = an
            mngr.service_n = tn

        else:
            assert (
                mngr.actor_n
                and
                mngr.service_tn
            )
            log.info(
                'Using extant service mngr!\n\n'
                f'{mngr!r}\n'  # it has a nice `.__repr__()` of services state
            )

        try:
            # NOTE: this is a singleton factory impl specific detail
            # which should be supported in the condensed
            # `@singleton_acm` API?
            mngr.debug_mode = debug_mode

            yield mngr
        finally:
            # TODO: is this more clever/efficient?
            # if 'samplerd' in mngr.service_ctxs:
            #     await mngr.cancel_service('samplerd')
            tn.cancel_scope.cancel()



def get_service_mngr() -> ServiceMngr:
    '''
    Try to get the singleton service-mngr for this actor presuming it
    has already been allocated using,

    .. code:: python

        async with open_<@singleton_acm(func)>() as mngr`
            ... this block kept open ...

    If not yet allocated raise a `ServiceError`.

    '''
    # https://stackoverflow.com/a/12627202
    # https://docs.python.org/3/library/inspect.html#inspect.Signature
    maybe_mngr: ServiceMngr|None = inspect.signature(
        open_service_mngr
    ).parameters['_singleton'].default[0]

    if maybe_mngr is None:
        raise RuntimeError(
            'Someone must allocate a `ServiceMngr` using\n\n'
            '`async with open_service_mngr()` beforehand!!\n'
        )

    return maybe_mngr


async def _open_and_supervise_service_ctx(
    serman: ServiceMngr,
    name: str,
    ctx_fn: Callable,  # TODO, type for `@tractor.context` requirement
    portal: Portal,

    allow_overruns: bool = False,
    task_status: TaskStatus[
        tuple[
            trio.CancelScope,
            Context,
            trio.Event,
            Any,
        ]
    ] = trio.TASK_STATUS_IGNORED,
    **ctx_kwargs,

) -> Any:
    '''
    Open a remote IPC-context defined by `ctx_fn` in the
    (service) actor accessed via `portal` and supervise the
    (local) parent task to termination at which point the remote
    actor runtime is cancelled alongside it.

    The main application is for allocating long-running
    "sub-services" in a main daemon and explicitly controlling
    their lifetimes from an actor-global singleton.

    '''
    # TODO: use the ctx._scope directly here instead?
    # -[ ] actually what semantics do we expect for this
    #   usage!?
    with trio.CancelScope() as cs:
        try:
            async with portal.open_context(
                ctx_fn,
                allow_overruns=allow_overruns,
                **ctx_kwargs,

            ) as (ctx, started):

                # unblock once the remote context has started
                complete = trio.Event()
                task_status.started((
                    cs,
                    ctx,
                    complete,
                    started,
                ))
                log.info(
                    f'`pikerd` service {name} started with value {started}'
                )
                # wait on any context's return value
                # and any final portal result from the
                # sub-actor.
                ctx_res: Any = await ctx.wait_for_result()

                # NOTE: blocks indefinitely until cancelled
                # either by error from the target context
                # function or by being cancelled here by the
                # surrounding cancel scope.
                return (
                    await portal.wait_for_result(),
                    ctx_res,
                )

        except ContextCancelled as ctxe:
            canceller: tuple[str, str] = ctxe.canceller
            our_uid: tuple[str, str] = current_actor().uid
            if (
                canceller != portal.chan.uid
                and
                canceller != our_uid
            ):
                log.cancel(
                    f'Actor-service `{name}` was remotely cancelled by a peer?\n'

                    # TODO: this would be a good spot to use
                    # a respawn feature Bo
                    f'-> Keeping `pikerd` service manager alive despite this inter-peer cancel\n\n'

                    f'cancellee: {portal.chan.uid}\n'
                    f'canceller: {canceller}\n'
                )
            else:
                raise

        finally:
            # NOTE: the ctx MUST be cancelled first if we
            # don't want the above `ctx.wait_for_result()` to
            # raise a self-ctxc. WHY, well since from the ctx's
            # perspective the cancel request will have
            # arrived out-out-of-band at the `Actor.cancel()`
            # level, thus `Context.cancel_called == False`,
            # meaning `ctx._is_self_cancelled() == False`.
            # with trio.CancelScope(shield=True):
            # await ctx.cancel()
            await portal.cancel_actor()  # terminate (remote) sub-actor
            complete.set()  # signal caller this task is done
            serman.service_ctxs.pop(name)  # remove mngr entry


# TODO: we need remote wrapping and a general soln:
# - factor this into a ``tractor.highlevel`` extension # pack for the
#   library.
# - wrap a "remote api" wherein you can get a method proxy
#   to the pikerd actor for starting services remotely!
# - prolly rename this to ActorServicesNursery since it spawns
#   new actors and supervises them to completion?
@dataclass
class ServiceMngr:
    '''
    A multi-subactor-as-service manager.

    Spawn, supervise and monitor service/daemon subactors in a SC
    process tree.

    '''
    actor_n: ActorNursery
    service_n: trio.Nursery
    debug_mode: bool = False # tractor sub-actor debug mode flag

    service_tasks: dict[
        str,
        tuple[
            trio.CancelScope,
            trio.Event,
        ]
    ] = field(default_factory=dict)

    service_ctxs: dict[
        str,
        tuple[
            trio.CancelScope,
            Context,
            Portal,
            trio.Event,
        ]
    ] = field(default_factory=dict)

    # internal per-service task mutexs
    _locks = defaultdict(trio.Lock)

    # TODO, unify this interface with our `TaskManager` PR!
    #
    #
    async def start_service_task(
        self,
        name: str,
        # TODO: typevar for the return type of the target and then
        # use it below for `ctx_res`?
        fn: Callable,

        allow_overruns: bool = False,
        **ctx_kwargs,

    ) -> tuple[
        trio.CancelScope,
        Any,
        trio.Event,
    ]:
        async def _task_manager_start(
            task_status: TaskStatus[
                tuple[
                    trio.CancelScope,
                    trio.Event,
                ]
            ] = trio.TASK_STATUS_IGNORED,
        ) -> Any:

            task_cs = trio.CancelScope()
            task_complete = trio.Event()

            with task_cs as cs:
                task_status.started((
                    cs,
                    task_complete,
                ))
                try:
                    await fn()
                except trio.Cancelled as taskc:
                    log.cancel(
                        f'Service task for `{name}` was cancelled!\n'
                        # TODO: this would be a good spot to use
                        # a respawn feature Bo
                    )
                    raise taskc
                finally:
                    task_complete.set()
        (
            cs,
            complete,
        ) = await self.service_n.start(_task_manager_start)

        # store the cancel scope and portal for later cancellation or
        # retstart if needed.
        self.service_tasks[name] = (
            cs,
            complete,
        )
        return (
            cs,
            complete,
        )

    async def cancel_service_task(
        self,
        name: str,

    ) -> Any:
        log.info(f'Cancelling `pikerd` service {name}')
        cs, complete = self.service_tasks[name]

        cs.cancel()
        await complete.wait()
        # TODO, if we use the `TaskMngr` from #346
        # we can also get the return value from the task!

        if name in self.service_tasks:
            # TODO: custom err?
            # raise ServiceError(
            raise RuntimeError(
                f'Service task {name!r} not terminated!?\n'
            )

    async def start_service_ctx(
        self,
        name: str,
        portal: Portal,
        # TODO: typevar for the return type of the target and then
        # use it below for `ctx_res`?
        ctx_fn: Callable,
        **ctx_kwargs,

    ) -> tuple[
        trio.CancelScope,
        Context,
        Any,
    ]:
        '''
        Start a remote IPC-context defined by `ctx_fn` in a background
        task and immediately return supervision primitives to manage it:

        - a `cs: CancelScope` for the newly allocated bg task
        - the `ipc_ctx: Context` to manage the remotely scheduled
          `trio.Task`.
        - the `started: Any` value returned by the remote endpoint
          task's `Context.started(<value>)` call.

        The bg task supervises the ctx such that when it terminates the supporting
        actor runtime is also cancelled, see `_open_and_supervise_service_ctx()`
        for details.

        '''
        cs, ipc_ctx, complete, started = await self.service_n.start(
            functools.partial(
                _open_and_supervise_service_ctx,
                serman=self,
                name=name,
                ctx_fn=ctx_fn,
                portal=portal,
                **ctx_kwargs,
            )
        )

        # store the cancel scope and portal for later cancellation or
        # retstart if needed.
        self.service_ctxs[name] = (cs, ipc_ctx, portal, complete)
        return (
            cs,
            ipc_ctx,
            started,
        )

    async def start_service(
        self,
        daemon_name: str,
        ctx_ep: Callable,  # kwargs must `partial`-ed in!
        # ^TODO, type for `@tractor.context` deco-ed funcs!

        debug_mode: bool = False,
        **start_actor_kwargs,

    ) -> Context:
        '''
        Start new subactor and schedule a supervising "service task"
        in it which explicitly defines the sub's lifetime.

        "Service daemon subactors" are cancelled (and thus
        terminated) using the paired `.cancel_service()`.

        Effectively this API can be used to manage "service daemons"
        spawned under a single parent actor with supervision
        semantics equivalent to a one-cancels-one style actor-nursery
        or "(subactor) task manager" where each subprocess's (and
        thus its embedded actor runtime) lifetime is synced to that
        of the remotely spawned task defined by `ctx_ep`.

        The funcionality can be likened to a "daemonized" version of
        `.hilevel.worker.run_in_actor()` but with supervision
        controls offered by `tractor.Context` where the main/root
        remotely scheduled `trio.Task` invoking `ctx_ep` determines
        the underlying subactor's lifetime.

        '''
        entry: tuple|None = self.service_ctxs.get(daemon_name)
        if entry:
            (cs, sub_ctx, portal, complete) = entry
            return sub_ctx

        if daemon_name not in self.service_ctxs:
            portal: Portal = await self.actor_n.start_actor(
                daemon_name,
                debug_mode=(  # maybe set globally during allocate
                    debug_mode
                    or
                    self.debug_mode
                ),
                **start_actor_kwargs,
            )
            ctx_kwargs: dict[str, Any] = {}
            if isinstance(ctx_ep, functools.partial):
                ctx_kwargs: dict[str, Any] = ctx_ep.keywords
                ctx_ep: Callable = ctx_ep.func

            (
                cs,
                sub_ctx,
                started,
            ) = await self.start_service_ctx(
                name=daemon_name,
                portal=portal,
                ctx_fn=ctx_ep,
                **ctx_kwargs,
            )

            return sub_ctx

    async def cancel_service(
        self,
        name: str,

    ) -> Any:
        '''
        Cancel the service task and actor for the given ``name``.

        '''
        log.info(f'Cancelling `pikerd` service {name}')
        cs, sub_ctx, portal, complete = self.service_ctxs[name]

        # cs.cancel()
        await sub_ctx.cancel()
        await complete.wait()

        if name in self.service_ctxs:
            # TODO: custom err?
            # raise ServiceError(
            raise RuntimeError(
                f'Service actor for {name} not terminated and/or unknown?'
            )

        # assert name not in self.service_ctxs, \
        #     f'Serice task for {name} not terminated?'
