import inspect
from typing import Any
from functools import partial
from contextlib import asynccontextmanager, AsyncExitStack
from itertools import cycle
from pprint import pformat

import trio
import tractor


log = tractor.log.get_logger(__name__)


class ActorState:
    """Singlteton actor per process.

    """
    # this is a class defined variable and is thus both
    # singleton across object instances and task safe.
    state: dict = {}

    def update(self, msg: dict) -> None:
        _actor = tractor.current_actor()

        print(f'Yo we got a message {msg}')
        self.state.update(msg)

        print(f'New local "state" for {_actor.uid} is {pformat(self.state)}')

    def close(self):
        # gives headers showing which process and task is active
        log.info('Actor state is closing')

    # if we wanted to support spawning or talking to other
    # actors we can do that using a portal map collection?
    # _portals: dict = {}


async def _run_proxy_method(
    meth: str,
    msg: dict,
) -> Any:
    """Update process-local state from sent message and exit.

    """
    # Create a new actor instance per call.
    # We can make this persistent by storing it either
    # in a global var or are another clas scoped variable?
    # If you want it somehow persisted in another namespace
    # I'd be interested to know "where".
    actor = ActorState()
    if meth != 'close':
        return getattr(actor, meth)(msg)
    else:
        actor.close()

    # we're done so exit this task running in the subactor


class MethodProxy:
    def __init__(
        self,
        portal: tractor._portal.Portal
    ) -> None:
        self._portal = portal

    async def _run_method(
        self,
        *,
        meth: str,
        msg: dict,
    ) -> Any:
        return await self._portal.run(
            _run_proxy_method,
            meth=meth,
            msg=msg
        )


def get_method_proxy(portal, target=ActorState) -> MethodProxy:

    proxy = MethodProxy(portal)

    # mock all remote methods
    for name, method in inspect.getmembers(
        target, predicate=inspect.isfunction
    ):
        if '_' == name[0]:
            # skip private methods
            continue

        else:
            setattr(proxy, name, partial(proxy._run_method, meth=name))

    return proxy


@asynccontextmanager
async def spawn_proxy_actor(name):

    # XXX: that subactor can **not** outlive it's parent, this is SC.
    async with tractor.open_nursery(
        debug_mode=True,
        # loglevel='info',
    ) as tn:

        portal = await tn.start_actor(name, enable_modules=[__name__])

        proxy = get_method_proxy(portal)

        yield proxy

        await proxy.close(msg=None)


async def main():
    # Main process/thread that spawns one sub-actor and sends messages
    # to it to update it's state.

    try:
        stack = AsyncExitStack()

        actors = []
        for name in ['even', 'odd']:

            actor_proxy = await stack.enter_async_context(
                    spawn_proxy_actor(name + '_boy')
            )
            actors.append(actor_proxy)

        # spin through the actors and update their states
        for i, (count, actor) in enumerate(
            zip(range(100), cycle(actors))
        ):
            # Here we call the locally patched `.update()` method of the
            # remote instance

            # NOTE: the instance created each call here is currently
            # a new object - to persist it across `portal.run()` calls
            # we need to store it somewhere in memory for access by
            # a new task spawned in the remote actor process.
            await actor.update(msg={f'msg_{i}': count})

    # blocks here indefinitely synce we spawned "daemon actors" using
    # .start_actor()`, you'll need to control-c to cancel.

    finally:
        await stack.aclose()


if __name__ == '__main__':
    trio.run(main)
