import trio
import tractor


async def main() -> None:
    '''
    Raise an assertion error from the debug-enabled root actor.

    '''
    async with tractor.open_root_actor(
        debug_mode=True,
    ):
        assert 0


if __name__ == '__main__':
    trio.run(main)
