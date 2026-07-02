'''
Run a *dedicated* registrar as its own standalone process — decoupled
from your app's root actor — and discover a service *through* it.

Normally the registrar **is** the root actor of your tree. Here we
instead boot a separate `tractor.run_daemon([], registry_addrs=[...])`
process whose *sole* job is to be the registry, then point our app
tree at it via `registry_addrs`. Because a registrar is already
reachable at that addr, our app's root actor does NOT become one — it
registers with (and discovers through) the external daemon. That's the
"registrar as a subsystem, not the root actor" pattern.

NB: `enable_transports` is single-proto per-runtime today (see
`tractor._root`), so this demos one transport; a genuinely
multi-backend registrar (and spawning one as a *sub*actor of a shared
tree) are future runtime work — see the #472 follow-ups.

'''
from contextlib import suppress
import signal
import socket
import subprocess
import sys
import time

import trio
import tractor


# the fixed addr the dedicated registrar binds and everyone points at.
REG_ADDR: tuple[str, int] = ('127.0.0.1', 1717)


def _wait_registrar_ready(
    addr: tuple[str, int],
    proc: subprocess.Popen,
    deadline: float = 10.0,
) -> None:
    '''
    Active-poll the registrar's bind addr until it accepts a
    connection (proving it's booted + listening), bailing early if
    the daemon proc dies during startup.

    '''
    end: float = time.monotonic() + deadline
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise RuntimeError(
                f'registrar died on startup (rc={proc.returncode})'
            )
        with suppress(OSError):
            with socket.create_connection(addr, timeout=0.1):
                return
        time.sleep(0.05)
    raise TimeoutError(f'registrar never came up @ {addr}')


async def greet() -> str:
    '''A trivial service task any peer can RPC by name.'''
    return f'hello from {tractor.current_actor().name}!'


async def app() -> None:
    '''
    Point our app tree at the EXTERNAL registrar (not its own root)
    via `registry_addrs`, register a named service, then discover +
    RPC it purely by name.

    '''
    an: tractor.ActorNursery
    async with tractor.open_nursery(
        registry_addrs=[REG_ADDR],
        enable_transports=['tcp'],
    ) as an:
        # this subactor registers with the DEDICATED registrar @
        # REG_ADDR (our root is a plain peer, not the registry).
        await an.start_actor(
            'greeter',
            enable_modules=[__name__],
        )
        # discover it *through the external registrar*, by name only.
        portal: tractor.Portal
        async with tractor.wait_for_actor('greeter') as portal:
            print(f'found `greeter` via dedicated registrar @ {REG_ADDR}')
            print(await portal.run(greet))
        await an.cancel()


def main() -> None:
    # boot the dedicated registrar as its own process/tree: an empty
    # `enable_modules` `run_daemon()` is just a root actor that does
    # nothing but hold + serve the registry.
    code: str = (
        'import tractor; '
        f'tractor.run_daemon([], registry_addrs={[REG_ADDR]!r}, '
        "enable_transports=['tcp'], loglevel='error')"
    )
    registrar: subprocess.Popen = subprocess.Popen(
        [sys.executable, '-c', code],
        # the registry is a quiet background service; hush its logs +
        # expected SIGINT-teardown traceback so the demo output stays
        # focused on the discovery flow.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_registrar_ready(REG_ADDR, registrar)
        print(
            f'dedicated registrar up @ {REG_ADDR} '
            f'(pid {registrar.pid})'
        )
        trio.run(app)
    finally:
        # graceful SIGINT teardown of the standalone registrar.
        registrar.send_signal(signal.SIGINT)
        with suppress(subprocess.TimeoutExpired):
            registrar.wait(timeout=10)
        print('dedicated registrar shut down')


if __name__ == '__main__':
    main()
