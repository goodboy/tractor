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


async def main():
    """Main tractor entry point, the "master" process (for now
    acts as the "director").
    """
    async with tractor.open_nursery() as an:
        print("Alright... Action!")

        # both actors wait on (then dial!) the *other*, so each
        # must outlive both hellos: spawn as daemons, run the
        # hellos concurrently, reap only once both complete.
        portals: dict[str, tractor.Portal] = {
            name: await an.start_actor(
                name,
                enable_modules=[__name__],
            )
            for name in ('donny', 'gretchen')
        }

        async def run_and_print(
            name: str,
            other_actor: str,
        ) -> None:
            print(
                # RPC through an existing actor's `Portal`.
                await portals[name].run(
                    say_hello,
                    other_actor=other_actor,
                )
            )

        async with trio.open_nursery() as tn:
            tn.start_soon(run_and_print, 'donny', 'gretchen')
            tn.start_soon(run_and_print, 'gretchen', 'donny')

        await an.cancel()

    print("CUTTTT CUUTT CUT!!! Donny!! You're supposed to say...")


if __name__ == '__main__':
    trio.run(main)
