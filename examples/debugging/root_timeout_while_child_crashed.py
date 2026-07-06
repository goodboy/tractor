import trio
import tractor


async def key_error():
    "Raise a ``NameError``"
    return {}['doggy']


async def main():
    '''
    Root is fail-after-cancelled while blocking and child RPC fails
    simultaneously.

    '''
    async with (
        tractor.open_nursery(
            debug_mode=True,
            # loglevel='debug'  # ?XXX required?
        ) as n,
        trio.open_nursery() as tn,
    ):
        # spawn the actor..
        portal = await n.start_actor(
            'key_error',
            enable_modules=[__name__],
        )
        print(
            f'Child is up @ {portal.chan.aid.reprol()}'
        )
        # ..then schedule its erroring task in the bg while the
        # root blocks below.
        tn.start_soon(portal.run, key_error)

        # XXX: originally a bug caused by this is where root would enter
        # the debugger and clobber the tty used by the repl even though
        # child should have it locked.
        with trio.fail_after(1):
            await trio.Event().wait()


if __name__ == '__main__':
    trio.run(main)
