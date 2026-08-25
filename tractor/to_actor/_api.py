# tractor: distributed structured concurrency.
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
One-shot remote-task invocation built on spawn-and-portal
primitives.

Implemented (as prescribed by #477) entirely "on top of"
the lower level daemon-actor spawn + portal APIs,

- `ActorNursery.start_actor()` for (daemon-style) subactor
  spawning,
- `Portal.open_context()` for scheduling the lone remote
  task with linked cancellation and waiting on its result,
- `Portal.cancel_actor()` for reaping the subactor once
  that result (or error) arrives,

such that error collection and propagation happens in the
*caller's task* (and thus whatever `trio` nursery/scope
encloses it) instead of inside the actor-nursery's
spawn-machinery nurseries as with the (to be deprecated)
`ActorNursery.run_in_actor()` API.

'''
from __future__ import annotations
import functools
import inspect
from typing import (
    Any,
    Awaitable,
    Callable,
    TYPE_CHECKING,
    TypeVar,
    TypeVarTuple,
    Unpack,
)

from .._context import (
    Context,
    context,
)
from ..msg.ptr import NamespacePath
from ..runtime._state import current_actor
from ..runtime._supervise import (
    ActorNursery,
    open_nursery,
)

if TYPE_CHECKING:
    from ..discovery._addr import UnwrappedAddress
    from ..runtime._portal import Portal


ArgsT = TypeVarTuple('ArgsT')
RetT = TypeVar('RetT')


def _validate_one_shot_fn(
    fn: Callable,
) -> None:
    '''
    Ensure `fn` is a non-streaming async function, raise
    a `TypeError` otherwise.

    The same constraint enforced by `Portal.open_context()` but
    checked up-front, BEFORE any subactor is spawned.

    '''
    if not (
        inspect.iscoroutinefunction(fn)
        and
        not getattr(
            fn,
            '_tractor_stream_function',
            False,
        )
    ):
        raise TypeError(
            f'{fn!r} must be a non-streaming async '
            f'function!'
        )


def _normalize_call(
    fn: Callable,
    args: tuple[Any, ...],
) -> tuple[
    Callable,
    tuple[Any, ...],
    dict[str, Any],
]:
    '''
    Normalize Trio-style positional and partial-bound arguments.

    Actor calls must send a namespace-addressable base function and
    serializable inputs to another process, so decompose partials and
    validate their complete call signature before runtime startup.

    '''
    kwargs: dict[str, Any] = {}
    while isinstance(fn, functools.partial):
        partial_args: tuple[Any, ...] = fn.args

        # `functools.Placeholder` was added in Python 3.14. Drop
        # this `getattr()` guard once 3.14 is the minimum version.
        if (
            (
                placeholder := getattr(
                    functools,
                    'Placeholder',
                    None,
                )
            ) is not None
            and
            any(
                arg is placeholder
                for arg in partial_args
            )
        ):
            call_args = iter(args)
            merged_args: list[Any] = []
            for arg in partial_args:
                if arg is placeholder:
                    try:
                        arg = next(call_args)
                    except StopIteration:
                        raise TypeError(
                            'Not enough positional arguments to '
                            'fill `functools.Placeholder`s'
                        ) from None

                merged_args.append(arg)

            merged_args.extend(call_args)
            args = tuple(merged_args)
        else:
            args = partial_args + args

        partial_kwargs = dict(fn.keywords or {})
        partial_kwargs.update(kwargs)
        kwargs = partial_kwargs
        fn = fn.func

    _validate_one_shot_fn(fn)
    inspect.signature(fn).bind(*args, **kwargs)
    return fn, args, kwargs


@context
async def _invoke_one_shot(
    ctx: Context,
    namespace: str,
    funcname: str,
    args: list[Any],
    kwargs: dict[str, Any],
) -> Any:
    '''
    Invoke an ordinary async function inside a linked IPC context.

    '''
    # Do not use `NamespacePath.load_ref()` here: target resolution
    # must remain behind the actor's RPC module allowlist.
    fn: Callable = current_actor()._get_rpc_func(
        namespace,
        funcname,
    )
    _validate_one_shot_fn(fn)
    await ctx.started()
    return await fn(*args, **kwargs)


async def _invoke_from_portal(
    portal: Portal,
    fn: Callable,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    '''
    Run `fn` through the context-linked one-shot endpoint.

    '''
    namespace, funcname = NamespacePath.from_ref(fn).to_tuple()
    async with portal.open_context(
        _invoke_one_shot,
        namespace=namespace,
        funcname=funcname,
        args=list(args),
        kwargs=kwargs,
    ) as (ctx, _):
        return await ctx.wait_for_result()


async def _invoke_in_subactor(
    an: ActorNursery,
    fn: Callable,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    name: str,
    spawn_kwargs: dict[str, Any],
) -> Any:
    '''
    Spawn a (daemon) subactor via `an.start_actor()`,
    schedule `fn` as its context-linked lone remote task and,
    ALWAYS, reap the subactor once that task's result (or error)
    has been delivered.

    '''
    portal: Portal = await an.start_actor(
        name,
        **spawn_kwargs,
    )
    try:
        return await _invoke_from_portal(
            portal,
            fn,
            args,
            kwargs,
        )
    finally:
        # Cancel and join this child before returning. The nursery
        # helper shields teardown, escalates a missed cancel ack and
        # waits for the child monitor to remove its process record.
        await an._cancel_and_reap_child(portal)


async def run(
    fn: Callable[[Unpack[ArgsT]], Awaitable[RetT]],
    *args: Unpack[ArgsT],

    # actor lifetime management: reuse an already-running peer
    # via its `portal: Portal`, spawn a fresh subactor from
    # a caller-managed `an: ActorNursery`, or, when
    # neither is provided, open a private actor-nursery
    # (implicitly booting the actor-runtime as needed)
    # scoped to just this call.
    portal: Portal|None = None,
    an: ActorNursery|None = None,

    # subactor spawn opts passed (mostly) verbatim to
    # `ActorNursery.start_actor()`; unused when `portal`
    # is provided.
    name: str|None = None,
    bind_addrs: list[UnwrappedAddress]|None = None,
    enable_modules: list[str]|None = None,
    loglevel: str|None = None,
    debug_mode: bool|None = None,
    infect_asyncio: bool = False,
    inherit_parent_main: bool = True,
    proc_kwargs: dict[str, Any]|None = None,

    # passed verbatim to the private `open_nursery()`
    # (and in turn any implicit `open_root_actor()`)
    # when NO `an`/`portal` is provided.
    runtime_kwargs: dict[str, Any]|None = None,

) -> RetT:
    '''
    Run the async `fn(*args)` as the lone task in a (new)
    subactor, block waiting on its result and return it; the
    distributed-parallelism equivalent of
    `trio.to_thread.run_sync()`.

    As with Trio's API, target arguments are positional. Use
    `functools.partial()` to bind target keyword arguments; all
    keyword arguments accepted here configure actor lifetime
    management, including actor reuse and spawning. A caller-supplied
    `portal` must address an actor started with both
    `tractor.to_actor.MODULE` and the target function's
    module in its `enable_modules` list. Calls that spawn their own
    actor add the trampoline module automatically.

    Unlike `ActorNursery.run_in_actor()` (which returns
    a `Portal` whose result is only collected at
    actor-nursery teardown) this is a plain "call and
    wait" primitive: any remote error is raised HERE, in
    the caller's task. Concurrency is composed the usual
    `trio` way by scheduling multiple `run()` calls in
    a local task nursery, ideally against a shared
    caller-managed `an: ActorNursery` (see the test
    suite for the canonical worker-pool-ish pattern).

    '''
    __runtimeframe__: int = 1  # noqa
    fn, args, kwargs = _normalize_call(fn, args)

    if (
        runtime_kwargs is not None
        and
        (
            an is not None
            or
            portal is not None
        )
    ):
        raise ValueError(
            '`runtime_kwargs` only applies when this '
            'call opens its own private actor-nursery '
            '(no `an`/`portal` provided)!'
        )

    if portal is not None:
        if an is not None:
            raise ValueError(
                'Pass at most ONE of `portal` or `an`, '
                'not both!'
            )
        return await _invoke_from_portal(
            portal,
            fn,
            args,
            kwargs,
        )

    name: str = name or fn.__name__
    spawn_kwargs: dict[str, Any] = dict(
        enable_modules=(
            [
                # The public `to_actor.MODULE` alias is only for
                # callers configuring an existing actor.
                __name__,
                fn.__module__,
            ]
            +
            (enable_modules or [])
        ),
        bind_addrs=bind_addrs,
        loglevel=loglevel,
        debug_mode=debug_mode,
        infect_asyncio=infect_asyncio,
        inherit_parent_main=inherit_parent_main,
        proc_kwargs=proc_kwargs,
    )
    if an is not None:
        return await _invoke_in_subactor(
            an,
            fn,
            args,
            kwargs,
            name,
            spawn_kwargs,
        )

    an: ActorNursery
    async with open_nursery(
        **(runtime_kwargs or {}),
    ) as an:
        return await _invoke_in_subactor(
            an,
            fn,
            args,
            kwargs,
            name,
            spawn_kwargs,
        )
