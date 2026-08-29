import trio
import tractor

tractor.log.get_console_log('INFO')


async def main(service_name: str) -> None:
    '''
    Discover one actor and inspect its registrar connection.

    '''
    an: tractor.ActorNursery
    async with tractor.open_nursery() as an:
        await an.start_actor(service_name)

        async with tractor.get_registry() as reg_portal:
            print(
                f'Registrar is listening on {reg_portal.channel}'
            )

        actor_portal: tractor.Portal
        async with tractor.wait_for_actor(
            service_name,
        ) as actor_portal:
            print(f'my_service is found at {actor_portal}')

        await an.cancel()


if __name__ == '__main__':
    trio.run(main, 'some_actor_name')
