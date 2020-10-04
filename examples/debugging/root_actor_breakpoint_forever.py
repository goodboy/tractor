import tractor


async def main():

    while True:
        await tractor.breakpoint()


if __name__ == '__main__':
    tractor.run(main, debug_mode=True)
