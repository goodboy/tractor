import trio
import tractor


async def movie_theatre_question() -> str:
    '''
    A question asked in a dark theatre, in a tangent
    (errr, I mean different) process.

    '''
    return 'have you ever seen a portal?'


async def main() -> None:
    '''
    The main ``tractor`` routine.

    '''
    an: tractor.ActorNursery
    async with tractor.open_nursery() as an:

        portal: tractor.Portal = await an.start_actor(
            'frank',
            # enable the actor to run funcs from this current module
            enable_modules=[__name__],
        )

        print(await portal.run(movie_theatre_question))
        # call the subactor a 2nd time
        print(await portal.run(movie_theatre_question))

        # the async with will wait indefinitely for "frank" because
        # its runtime remains active until explicitly cancelled
        await portal.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
