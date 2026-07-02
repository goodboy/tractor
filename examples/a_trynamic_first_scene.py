import trio
import tractor

_this_module = __name__
the_line = 'Hi my name is {}'


tractor.log.get_console_log("INFO")


async def hi() -> str:
    return the_line.format(tractor.current_actor().name)


async def say_hello(other_actor: str) -> str:
    portal: tractor.Portal
    async with tractor.wait_for_actor(other_actor) as portal:
        return await portal.run(hi)


async def main() -> None:
    """Main tractor entry point, the "master" process (for now
    acts as the "director").
    """
    n: tractor.ActorNursery
    async with tractor.open_nursery() as n:
        print("Alright... Action!")

        donny: tractor.Portal = await n.run_in_actor(
            say_hello,
            name='donny',
            # arguments are always named
            other_actor='gretchen',
        )
        gretchen: tractor.Portal = await n.run_in_actor(
            say_hello,
            name='gretchen',
            other_actor='donny',
        )
        print(await gretchen.wait_for_result())
        print(await donny.wait_for_result())
        print("CUTTTT CUUTT CUT!!! Donny!! You're supposed to say...")


if __name__ == '__main__':
    trio.run(main)
