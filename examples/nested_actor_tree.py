'''
Demonstrate a (3-level) nested actor tree where one RPC from
the root fans out through a mid-tier 'supervisor' actor to
2 'leaf' worker actors and an aggregate result is relayed
back up.

The process tree should look approximately like:

python examples/nested_actor_tree.py
`-python -m tractor._child --uid ('supervisor', '7c9b1039 ..)
  |-python -m tractor._child --uid ('leaf_1', '92d62f50 ..)
  `-python -m tractor._child --uid ('leaf_2', 'de91fdf5 ..)

Teardown runs inside-out: the supervisor cancels its leaves
first, then the root cancels the supervisor; watch the
prints to see the ordering.

'''
import trio
import tractor


async def compute_square(x: int) -> int:
    '''
    Tiny "work unit" run inside a leaf actor.

    '''
    name: str = tractor.current_actor().name
    print(f'{name}: squaring {x}')
    return x * x


@tractor.context
async def fan_out_squares(
    ctx: tractor.Context,
    vals: list[int],
) -> list[int]:
    '''
    Spawn a (nested) pair of leaf actors, fan the input vals
    out across them round-robin style, then return the
    aggregated squares to our parent.

    '''
    an: tractor.ActorNursery
    async with tractor.open_nursery() as an:
        portals: list[tractor.Portal] = []
        for i in (1, 2):
            portals.append(
                await an.start_actor(
                    f'leaf_{i}',
                    enable_modules=[__name__],
                )
            )
        # unblock the parent's `.open_context()` entry and
        # report which leaves came up.
        await ctx.started(
            [p.chan.aid.name for p in portals]
        )
        squares: dict[int, int] = {}

        async def run_in_leaf(
            portal: tractor.Portal,
            x: int,
        ) -> None:
            squares[x] = await portal.run(
                compute_square,
                x=x,
            )

        # fan out one sub-RPC per input val, concurrently.
        tn: trio.Nursery
        async with trio.open_nursery() as tn:
            for i, x in enumerate(vals):
                tn.start_soon(
                    run_in_leaf,
                    portals[i % len(portals)],
                    x,
                )
        # graceful inside-out teardown: leaves go first!
        for portal in portals:
            leaf_name: str = portal.chan.aid.name
            print(f'supervisor: cancelling {leaf_name}')
            await portal.cancel_actor()
    return [squares[x] for x in vals]


async def main() -> None:
    an: tractor.ActorNursery
    async with tractor.open_nursery() as an:
        portal: tractor.Portal = await an.start_actor(
            'supervisor',
            enable_modules=[__name__],
        )
        async with portal.open_context(
            fan_out_squares,
            vals=[1, 2, 3, 4],
        ) as (ctx, leaf_names):
            print(f'root: supervisor spawned {leaf_names}')
            squares: list[int] = await ctx.wait_for_result()
            assert squares == [1, 4, 9, 16]
            print(f'root: aggregate result {squares}')
        print('root: cancelling supervisor')
        await portal.cancel_actor()
    print('root: tree torn down, what zombies?')


if __name__ == '__main__':
    trio.run(main)
