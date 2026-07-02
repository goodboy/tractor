'''
Demonstrate the "service daemon" pattern: a named,
long-lived actor spawned via `ActorNursery.start_actor()`
which any other task can locate through the registrar using
`tractor.find_actor()` / `tractor.wait_for_actor()` - no
spawn-portal required - and RPC into directly.

Teardown is explicit and graceful via `portal.cancel_actor()`
once the clients are done.

'''
import trio
import tractor

_quotes: dict[str, float] = {
    'btcusdt': 66_000.5,
    'ethusdt': 3_500.25,
}


async def get_quote(sym: str) -> float:
    '''
    Look up the "current" quote for a symbol.

    '''
    name: str = tractor.current_actor().name
    print(f'{name}: serving quote for {sym!r}')
    return _quotes[sym]


async def client_task() -> None:
    '''
    Locate the quote service by name and RPC it; note no
    spawn-nursery/portal reference is ever passed in here!

    '''
    # a lookup miss yields `None` (not an error).
    async with tractor.find_actor('no_such_svc') as portal:
        assert portal is None
        print('client: "no_such_svc" is not registered')
    # block until the service shows up in the registry,
    # then call into it through the delivered portal.
    async with tractor.wait_for_actor('quote_svc') as portal:
        quote: float = await portal.run(
            get_quote,
            sym='btcusdt',
        )
        print(f'client: got btcusdt quote {quote}')


async def main() -> None:
    an: tractor.ActorNursery
    async with tractor.open_nursery() as an:
        portal: tractor.Portal = await an.start_actor(
            'quote_svc',
            enable_modules=[__name__],
        )
        # run the client in a separate task which discovers
        # the daemon purely by its registered name.
        tn: trio.Nursery
        async with trio.open_nursery() as tn:
            tn.start_soon(client_task)
        # explicit graceful teardown of the daemon.
        print('root: cancelling quote_svc')
        await portal.cancel_actor()
    print('root: service shut down cleanly')


if __name__ == '__main__':
    trio.run(main)
