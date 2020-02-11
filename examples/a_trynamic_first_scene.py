import tractor

_this_module = __name__
the_line = 'Hi my name is {}'


tractor.log.get_console_log("INFO")


async def hi():
    return the_line.format(tractor.current_actor().name)


async def say_hello(other_actor):
    async with tractor.wait_for_actor(other_actor) as portal:
        return await portal.run(_this_module, 'hi')


async def main():
    """Main tractor entry point, the "master" process (for now
    acts as the "director").
    """
    async with tractor.open_nursery() as n:
        print("Alright... Action!")

        donny = await n.run_in_actor(
            'donny',
            say_hello,
            # arguments are always named
            other_actor='gretchen',
        )
        gretchen = await n.run_in_actor(
            'gretchen',
            say_hello,
            other_actor='donny',
        )
        print(await gretchen.result())
        print(await donny.result())
        print("CUTTTT CUUTT CUT!!! Donny!! You're supposed to say...")


if __name__ == '__main__':
    tractor.run(main)
