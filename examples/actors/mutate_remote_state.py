from itertools import cycle
from pprint import pformat
from dataclasses import dataclass, field

import trio
import tractor


@dataclass
class MyProcessStateThing:
    state: dict = field(default_factory=dict)

    def update(self, msg: dict):
        self.state.update(msg)


_actor_state = MyProcessStateThing()


async def update_local_state(msg: dict):
    """Update process-local state from sent message and exit.

    """
    actor = tractor.current_actor()

    global _actor_state


    print(f'Yo we got a message {msg}')

    # update the "actor state"
    _actor_state.update(msg)

    print(f'New local "state" for {actor.uid} is {pformat(_actor_state.state)}')

    # we're done so exit this task running in the subactor


async def main():
    # Main process/thread that spawns one sub-actor and sends messages
    # to it to update it's state.

    actor_portals = []

    # XXX: that subactor can **not** outlive it's parent, this is SC.
    async with tractor.open_nursery() as tn:

        portal = await tn.start_actor('even_boy', enable_modules=[__name__])
        actor_portals.append(portal)

        portal = await tn.start_actor('odd_boy', enable_modules=[__name__])
        actor_portals.append(portal)

        for i, (count, portal) in enumerate(
            zip(range(100), cycle(actor_portals))
        ):
            await portal.run(update_local_state, msg={f'msg_{i}': count})

    # blocks here indefinitely synce we spawned "daemon actors" using
    # .start_actor()`, you'll need to control-c to cancel.


if __name__ == '__main__':
    trio.run(main)
