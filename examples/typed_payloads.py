'''
Demonstrate "typed messaging": applying a `msgspec.Struct`
payload-type-spec to an IPC context via
`@tractor.context(pld_spec=...)`.

The child's `ctx.started()` value is stringently (round-trip)
validated against the spec *on the send side*, so a mistyped
payload raises a `tractor.MsgTypeError` before it ever hits
the wire; stream payloads are checked on `receive()` and
decode natively to the struct type.

'''
from msgspec import Struct

import trio
import tractor


class Point(Struct):
    '''
    A simple 2D-coordinate msg-payload type.

    '''
    x: int
    y: int


@tractor.context(pld_spec=Point|None)
async def point_doubler(
    ctx: tractor.Context,
) -> None:
    '''
    Stream back each received `Point` with doubled fields.

    '''
    # deliberately send a non-`Point` as our started-value;
    # the send-side pld-spec validation catches it locally
    # BEFORE anything is shipped over IPC.
    try:
        await ctx.started('this is no Point..')
    except tractor.MsgTypeError:
        print(
            'child: just as planned, a `str` payload failed '
            'the `Point|None` pld-spec B)'
        )
    # now do it right; the parent receives this as the 2nd
    # element of its `.open_context()` entry tuple.
    await ctx.started(Point(x=0, y=0))
    async with ctx.open_stream() as stream:
        async for pt in stream:
            # natively decoded to our struct type!
            assert type(pt) is Point
            await stream.send(
                Point(
                    x=pt.x * 2,
                    y=pt.y * 2,
                )
            )


async def main() -> None:
    an: tractor.ActorNursery
    async with tractor.open_nursery() as an:
        portal: tractor.Portal = await an.start_actor(
            'point_doubler',
            enable_modules=[__name__],
        )
        async with (
            portal.open_context(
                point_doubler,
            ) as (ctx, first),
            ctx.open_stream() as stream,
        ):
            # the (validated) started-value from the child
            assert first == Point(x=0, y=0)
            for i in range(3):
                await stream.send(Point(x=i, y=i))
                doubled: Point = await stream.receive()
                assert doubled == Point(x=i * 2, y=i * 2)
                print(f'parent: rx {doubled}')
        # explicitly teardown the daemon-actor
        await portal.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
