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
- `Portal.run()` for scheduling the lone remote task and
  waiting on its result,
- `Portal.cancel_actor()` for reaping the subactor once
  that result (or error) arrives,

such that error collection and propagation happens in the
*caller's task* (and thus whatever `trio` nursery/scope
encloses it) instead of inside the actor-nursery's
spawn-machinery nurseries as with the (to be deprecated)
`ActorNursery.run_in_actor()` API.

'''
from __future__ import annotations
import inspect
from typing import (
    Any,
    Callable,
    TYPE_CHECKING,
)

from ..runtime._supervise import (
    ActorNursery,
    open_nursery,
)

if TYPE_CHECKING:
    from ..discovery._addr import UnwrappedAddress
    from ..runtime._portal import Portal


def _validate_one_shot_fn(
    fn: Callable,
) -> None:
    '''
    Ensure `fn` is a non-streaming async function, raise
    a `TypeError` otherwise.

    The same constraint enforced by `Portal.run()` but
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


async def _invoke_in_subactor(
    an: ActorNursery,
    fn: Callable,
    name: str,
    spawn_kwargs: dict[str, Any],
    fn_kwargs: dict[str, Any],
) -> Any:
    '''
    Spawn a (daemon) subactor via `an.start_actor()`,
    schedule `fn` as its lone remote task via
    `Portal.run()` and, ALWAYS, reap the subactor once
    that task's result (or error) has been delivered.

    '''
    portal: Portal = await an.start_actor(
        name,
        **spawn_kwargs,
    )
    try:
        return await portal.run(
            fn,
            **fn_kwargs,
        )
    finally:
        # one-shot semantics: the subactor's lifetime is
        # bound to its lone task's completion; the
        # cancel-req's bounded wait is shielded
        # internally (see `Portal.cancel_actor()`) so
        # this reap also runs when the caller's scope
        # was itself cancelled.
        await portal.cancel_actor()


async def run(
    fn: Callable,
    *,

    # actor "placement": reuse an already-running peer
    # via its `portal`, spawn a fresh subactor from
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

    **fn_kwargs,  # explicit (keyword) args to `fn`

) -> Any:
    '''
    Run the async `fn` as the lone task in a (new)
    subactor, block waiting on its result and return it;
    the distributed-parallelism equivalent of
    `trio.to_thread.run_sync()`.

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
    _validate_one_shot_fn(fn)

    if (
        runtime_kwargs
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
        return await portal.run(
            fn,
            **fn_kwargs,
        )

    name: str = name or fn.__name__
    spawn_kwargs: dict[str, Any] = dict(
        enable_modules=(
            [fn.__module__]
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
            name,
            spawn_kwargs,
            fn_kwargs,
        )

    async with open_nursery(
        **(runtime_kwargs or {}),
    ) as an:
        return await _invoke_in_subactor(
            an,
            fn,
            name,
            spawn_kwargs,
            fn_kwargs,
        )
