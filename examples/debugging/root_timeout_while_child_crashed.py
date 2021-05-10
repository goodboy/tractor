
import trio
import tractor


async def key_error():
    "Raise a ``NameError``"
    return {}['doggy']


async def main():
    """Root dies 

    """
    async with tractor.open_nursery(
        debug_mode=True,
        loglevel='debug'
    ) as n:

        # spawn both actors
        portal = await n.run_in_actor(key_error)

        # XXX: originally a bug causes by this
        # where root would enter debugger even
        # though child should have it locked.
        with trio.fail_after(1):
            await trio.Event().wait()


if __name__ == '__main__':
    trio.run(main)
