import trio
import tractor

_this_module = __name__
the_line = 'Hi my name is {}'


tractor.log.get_console_log("INFO")


async def hi():
    return the_line.format(tractor.current_actor().name)


async def say_hello(other_actor):
    async with tractor.wait_for_actor(other_actor) as portal:
        return await portal.run(hi)


async def run_and_print(
    an: tractor.ActorNursery,
    name: str,
    other_actor: str,
):
    print(
        await tractor.to_actor.run(
            say_hello,
            an=an,
            name=name,
            # arguments are always named
            other_actor=other_actor,
        )
    )


async def main():
    """Main tractor entry point, the "master" process (for now
    acts as the "director").
    """
    async with (
        tractor.open_nursery() as an,
        trio.open_nursery() as tn,
    ):
        print("Alright... Action!")

        # both actors wait on the *other* to register so their
        # one-shots must run concurrently.
        tn.start_soon(run_and_print, an, 'donny', 'gretchen')
        tn.start_soon(run_and_print, an, 'gretchen', 'donny')

    print("CUTTTT CUUTT CUT!!! Donny!! You're supposed to say...")


if __name__ == '__main__':
    trio.run(main)
