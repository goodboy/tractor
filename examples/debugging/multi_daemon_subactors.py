import tractor
import trio


async def breakpoint_forever():
    "Indefinitely re-enter debugger in child actor."
    while True:
        yield 'yo'
        await tractor.breakpoint()


async def name_error():
    "Raise a ``NameError``"
    getattr(doggypants)


async def main():
    """Test breakpoint in a streaming actor.
    """
    async with tractor.open_nursery() as n:

        p0 = await n.start_actor('bp_forever', rpc_module_paths=[__name__])
        p1 = await n.start_actor('name_error', rpc_module_paths=[__name__])

        # retreive results
        stream = await p0.run(__name__, 'breakpoint_forever')
        await p1.run(__name__, 'name_error')


if __name__ == '__main__':
    tractor.run(main, debug_mode=True, loglevel='error')
