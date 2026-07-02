'''
Demonstrate an actor tree which talks over unix-domain-socket
(UDS) transport instead of the default TCP: pass
`enable_transports=['uds']` when opening the root and every
subactor inherits the preference.

Every channel address is a filesystem socket path (no TCP port
in sight!) and, as a kernel-provided bonus, the peer's pid is
exchanged for free via `SO_PEERCRED` on linux,
`LOCAL_PEERPID` on macOS.

'''
import os

import trio
import tractor


async def report_addr() -> str:
    '''
    Return this actor's own accept (bind) addr + pid.

    '''
    actor = tractor.current_actor()
    addr: tuple = actor.accept_addr
    pid: int = os.getpid()
    return f'{actor.name}@{addr} pid={pid}'


async def main() -> None:
    async with tractor.open_nursery(
        enable_transports=['uds'],
    ) as an:
        portal = await an.start_actor(
            'uds_child',
            enable_modules=[__name__],
        )
        # the channel's remote addr is a `UDSAddress`: a
        # filesystem socket path, NOT a (host, port) pair!
        raddr = portal.chan.raddr
        assert raddr.proto_key == 'uds'
        # NOTE, `.sockpath` is the *shared listener* socket file
        # (named for the root registrar) this channel rode in
        # on, NOT a per-child path; the child-specific identity
        # we get for free is the kernel-reported peer pid (via
        # `SO_PEERCRED` on linux, `LOCAL_PEERPID` on macOS).
        print(
            f'portal chan tpt proto: {raddr.proto_key!r}\n'
            f'listener sock file: {raddr.sockpath}\n'
            f'kernel-reported peer pid: {raddr.maybe_pid}\n'
        )
        # ask the child for its OWN distinct bind addr: another
        # socket-file path under the runtime dir.
        print(f'child says: {await portal.run(report_addr)}')
        await portal.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
