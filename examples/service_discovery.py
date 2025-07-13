import trio
import tractor

tractor.log.get_console_log("INFO")


async def main(service_name):

    async with tractor.open_nursery() as an:
        await an.start_actor(service_name)

        async with tractor.get_registry() as portal:
            print(f"Arbiter is listening on {portal.channel}")

        async with tractor.wait_for_actor(service_name) as sockaddr:
            print(f"my_service is found at {sockaddr}")

        await an.cancel()


if __name__ == '__main__':
    trio.run(main, 'some_actor_name')
