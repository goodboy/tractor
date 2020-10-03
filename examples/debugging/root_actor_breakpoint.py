import trio
import tractor


async def main():

    await trio.sleep(0.1)

    await tractor.breakpoint()

    await trio.sleep(0.1)


if __name__ == '__main__':
    tractor.run(main, debug_mode=True)
