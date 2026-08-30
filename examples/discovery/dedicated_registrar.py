'''
Run a dedicated registrar in a standalone process.

The service and discovery client are sibling actors. The client has
no pre-existing channel to the service, so its lookup must use the
external registrar instead of the local-peer fast path.

'''
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager as acm
import errno
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

import trio
import tractor


MAX_BIND_ATTEMPTS: int = 5


def _is_addr_collision(exc: BaseException) -> bool:
    '''
    Return whether registrar startup lost the selected TCP address.

    Tractor can notice the collision while probing the address or
    later when its listener binds. Exception groups are retryable
    only when every contained failure reports the same collision.

    '''
    match exc:
        case BaseExceptionGroup(exceptions=exceptions):
            return bool(exceptions) and all(
                _is_addr_collision(child)
                for child in exceptions
            )

        case OSError() as os_error:
            return (
                os_error.errno in {errno.EADDRINUSE, 10048}
                or getattr(os_error, 'winerror', None) == 10048
            )

        case RuntimeError() as runtime_error:
            message: str = str(runtime_error)
            return (
                'Registry address(es) are occupied' in message
                or 'registry socket(s) already bound' in message
            )

        case _:
            return False


def run_registrar(ready_path: str) -> None:
    '''
    Serve as the required registrar and report its selected address.

    The kernel selects ephemeral loopback candidates in this process.
    If another process claims a released candidate first, retry with
    a fresh candidate up to `MAX_BIND_ATTEMPTS`. Other startup errors
    and the final collision remain visible. `ensure_registry=True`
    prevents silently joining a registrar that won the address.

    '''
    ready_file: Path = Path(ready_path)

    async def serve() -> None:
        '''
        Open the registrar, publish readiness, and serve forever.

        '''
        for attempt in range(1, MAX_BIND_ATTEMPTS + 1):
            # This selector socket reserves and reports a
            # kernel-selected candidate; it never listens and is
            # not transferred to Tractor. Closing it lets
            # `open_root_actor()` create its own listener on the
            # same addr. The close/rebind handoff is non-atomic,
            # hence the bounded collision retries.
            sock: socket.socket
            with socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM,
            ) as sock:
                sock.bind(('127.0.0.1', 0))
                selected: tuple[str, int] = sock.getsockname()
                registry_addr: tuple[str, int] = (
                    selected[0],
                    selected[1],
                )

            try:
                actor: tractor.Actor
                async with tractor.open_root_actor(
                    name='dedicated_registrar',
                    registry_addrs=[registry_addr],
                    enable_transports=['tcp'],
                    enable_modules=[],
                    ensure_registry=True,
                    loglevel='error',
                ) as actor:
                    if not actor.is_registrar:
                        raise RuntimeError(
                            'daemon did not become registrar'
                        )

                    tmp_file: Path = ready_file.with_suffix('.tmp')
                    tmp_file.write_text(
                        str(registry_addr[1]),
                        encoding='ascii',
                    )
                    tmp_file.replace(ready_file)
                    await trio.sleep_forever()

            except BaseException as exc:
                if (
                    not _is_addr_collision(exc)
                    or attempt == MAX_BIND_ATTEMPTS
                ):
                    raise
                await trio.sleep(.05 * attempt)

    try:
        trio.run(serve)
    except KeyboardInterrupt:
        pass


def _registrar_command(ready_path: Path) -> list[str]:
    '''
    Build a child command that loads without running `main()`.

    `runpy.run_path()` also works when the docs test copies and
    renames this example before executing it.

    '''
    module_path: str = repr(str(Path(__file__).resolve()))
    function_name: str = repr('run_registrar')
    ready_arg: str = repr(str(ready_path))
    code: str = (
        f'import runpy; module = runpy.run_path({module_path}); '
        f'module[{function_name}]({ready_arg})'
    )
    return [sys.executable, '-c', code]


def _wait_registrar_ready(
    ready_path: Path,
    proc: subprocess.Popen,
    deadline: float = 10.0,
) -> tuple[str, int]:
    '''
    Wait until the child has entered its registrar actor context.

    The child atomically publishes its selected port only after
    `open_root_actor()` completes. Fail early if startup crashes.

    '''
    end: float = time.monotonic() + deadline
    while time.monotonic() < end:
        if proc.poll() is not None:
            returncode: int|None = proc.returncode
            raise RuntimeError(
                f'registrar exited during startup: {returncode=}'
            )

        try:
            port: int = int(
                ready_path.read_text(encoding='ascii')
            )
        except (
            OSError,
            ValueError,
        ):
            time.sleep(.05)
            continue

        if not 0 < port < 2**16:
            raise RuntimeError(f'invalid registrar port: {port!r}')
        if proc.poll() is not None:
            raise RuntimeError(
                'registrar exited after reporting ready'
            )
        return ('127.0.0.1', port)

    raise TimeoutError('registrar did not report ready')


def _stop_registrar(
    proc: subprocess.Popen,
    graceful_timeout: float = 5.0,
) -> None:
    '''
    Stop and reap the registrar, escalating after a bounded wait.

    Windows children receive `CTRL_C_EVENT` in their new process
    group; POSIX children receive `SIGINT`. A child that ignores
    graceful shutdown is killed, and every path finishes with
    `wait()`. A non-zero child exit remains visible to the caller.

    '''
    if proc.poll() is None:
        graceful_signal: int = (
            signal.CTRL_C_EVENT
            if sys.platform == 'win32'
            else signal.SIGINT
        )
        try:
            proc.send_signal(graceful_signal)
        except OSError:
            if proc.poll() is None:
                proc.terminate()

    try:
        proc.wait(timeout=graceful_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    if proc.returncode:
        raise RuntimeError(
            'registrar shutdown failed: '
            f'returncode={proc.returncode}'
        )


async def greet() -> str:
    '''
    Return a greeting identifying the actor serving the RPC.

    '''
    actor_name: str = tractor.current_actor().name
    return f'hello from {actor_name}!'


async def discover_and_greet(
    registry_addr: tuple[str, int],
) -> tuple[str, str, str]:
    '''
    Prove registrar lookup from a client without a service channel.

    The parent spawns this actor as `greeter`'s sibling. A non-`None`
    registry portal from `query_actor()` proves that discovery did
    not take the existing-peer fast path, which returns no registry
    portal.

    '''
    service_addr: tuple[str, int]|None
    registry_portal: tractor.Portal|None
    async with tractor.query_actor(
        'greeter',
        regaddr=registry_addr,
    ) as (service_addr, registry_portal):
        if registry_portal is None:
            raise RuntimeError('lookup used a local service channel')
        if service_addr is None:
            raise RuntimeError('greeter was not registered')

    service_portal: tractor.Portal|None
    async with tractor.find_actor(
        'greeter',
        registry_addrs=[registry_addr],
    ) as service_portal:
        if service_portal is None:
            raise RuntimeError('greeter disappeared before RPC')
        greeting: str = await service_portal.run(greet)

    client_name: str = tractor.current_actor().name
    return client_name, repr(service_addr), greeting


async def app(registry_addr: tuple[str, int]) -> None:
    '''
    Use sibling service and client actors with an external registrar.

    Only the parent receives both spawn-time portals. The `client`
    actor performs discovery in its own process and has no direct
    `greeter` channel before the lookup.

    '''
    actor_nursery: tractor.ActorNursery
    async with tractor.open_nursery(
        registry_addrs=[registry_addr],
        enable_transports=['tcp'],
    ) as actor_nursery:
        await actor_nursery.start_actor(
            'greeter',
            enable_modules=[__name__],
        )
        client_portal: tractor.Portal = (
            await actor_nursery.start_actor(
                'client',
                enable_modules=[__name__],
            )
        )
        result: tuple[str, str, str] = await client_portal.run(
            discover_and_greet,
            registry_addr=registry_addr,
        )
        client_name: str
        service_addr: str
        greeting: str
        (
            client_name,
            service_addr,
            greeting,
        ) = result
        print(
            f'{client_name!r} found `greeter` through registrar '
            f'{registry_addr!r}; service address: {service_addr}\n'
            f'{greeting}'
        )
        await actor_nursery.cancel()


# TODO: Promote this lifecycle into an OTB `tractor.discovery`
# registrar subsystem. Reuse attach-or-create ownership from
# `piker.service.maybe_open_pikerd()` and named service supervision
# from `piker.service.Services`; replace the file readiness
# handshake, then use the API from `tractor._testing.pytest` to
# isolate remaining hard-coded `reg_addr` cases.
@acm
async def _open_registrar(
) -> AsyncIterator[tuple[str, int]]:
    '''
    Start, publish, and reap one dedicated registrar process.

    The Windows child gets a distinct console process group so the
    graceful control event targets it without interrupting this
    process.

    '''
    temp_dir: str
    with tempfile.TemporaryDirectory(
        prefix='tractor-registrar-',
    ) as temp_dir:
        ready_path: Path = Path(temp_dir) / 'ready'
        creationflags: int = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            if sys.platform == 'win32'
            else 0
        )
        registrar: subprocess.Popen = subprocess.Popen(
            _registrar_command(ready_path),
            stdout=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        primary_error: BaseException|None = None
        try:
            registry_addr: tuple[str, int] = _wait_registrar_ready(
                ready_path,
                registrar,
            )
            print(
                f'dedicated registrar ready at {registry_addr!r} '
                f'(pid {registrar.pid})'
            )
            yield registry_addr
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                _stop_registrar(registrar)
            except BaseException as cleanup_error:
                if primary_error is None:
                    raise
                cleanup_note: str = (
                    'registrar cleanup also failed: '
                    f'{cleanup_error!r}'
                )
                primary_error.add_note(cleanup_note)
            print('dedicated registrar shut down')


async def main() -> None:
    '''
    Run the external registrar and sibling discovery actors.

    '''
    registry_addr: tuple[str, int]
    async with _open_registrar() as registry_addr:
        await app(registry_addr)


if __name__ == '__main__':
    trio.run(main)
