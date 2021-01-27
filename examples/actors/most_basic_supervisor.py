import trio
import tractor


class Restart(Exception):
    """Restart signal"""


async def sleep_then_restart():
    actor = tractor.current_actor()
    print(f'{actor.uid} starting up!')
    await trio.sleep(0.5)
    raise Restart('This is a restart signal')


async def signal_restart_whole_actor():
    actor = tractor.current_actor()
    print(f'{actor.uid} starting up!')
    await trio.sleep(0.5)
    return 'restart_me'


async def respawn_remote_task(portal):
    # start a task in the actor at the other end
    # of the provided portal, when it signals a restart,
    # restart it..

    # This is much more efficient then restarting the undlerying
    # process over and over since the python interpreter runtime
    # stays up and we just submit a new task to run (which
    # is just the original one we submitted repeatedly.
    while True:
        try:
            await portal.run(sleep_then_restart)
        except tractor.RemoteActorError as error:
            if 'Restart' in str(error):
                # respawn the actor task
                continue


async def supervisor():

    async with tractor.open_nursery() as tn:

        p0 = await tn.start_actor('task_restarter', enable_modules=[__name__])

        # Yes, you can do this from multiple tasks on one actor
        # or mulitple lone tasks in multiple subactors.
        # We'll show both.

        async with trio.open_nursery() as n:
            # we'll doe the first as a lone task restart in a daemon actor
            for i in range(4):
                n.start_soon(respawn_remote_task, p0)

            # Open another nursery that will respawn sub-actors

            # spawn a set of subactors that will signal restart
            # of the group of processes on each failures
            portals = []

            # start initial subactor set
            for i in range(4):
                p = await tn.run_in_actor(signal_restart_whole_actor)
                portals.append(p)

            # now wait on results and respawn actors
            # that request it
            while True:

                for p in portals:
                    result = await p.result()

                    if result == 'restart_me':
                        print(f'restarting {p.channel.uid}')
                        await p.cancel_actor()
                        await trio.sleep(0.5)
                        p = await tn.run_in_actor(signal_restart_whole_actor)
                        portals.append(p)

    # this will block indefinitely so user must
    # cancel with ctrl-c


if __name__ == '__main__':
    trio.run(supervisor)
