# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public
# License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later
# version.

# This program is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
# PURPOSE. See the GNU Affero General Public License for more
# details.

# You should have received a copy of the GNU Affero General
# Public License along with this program. If not, see
# <https://www.gnu.org/licenses/>.

'''
Actor-registry for process-tree service discovery.

The `Registrar` is a special `Actor` subtype that serves as
the process-tree's name-registry, tracking actor
name-to-address mappings so peers can discover each other.

'''
from __future__ import annotations

from bidict import bidict
import trio

from ..runtime._runtime import Actor
from ._addr import (
    UnwrappedAddress,
    Address,
    wrap_address,
)
from ..devx import debug
from ..log import get_logger


log = get_logger('tractor')


class Registrar(Actor):
    '''
    A special registrar `Actor` who can contact all other
    actors within its immediate process tree and keeps
    a registry of others meant to be discoverable in
    a distributed application.

    Normally the registrar is also the "root actor" and
    thus always has access to the top-most-level actor
    (process) nursery.

    By default, the registrar is always initialized when
    and if no other registrar socket addrs have been
    specified to runtime init entry-points (such as
    `open_root_actor()` or `open_nursery()`). Any time
    a new main process is launched (and thus a new root
    actor created) and, no existing registrar can be
    contacted at the provided `registry_addr`, then
    a new one is always created; however, if one can be
    reached it is used.

    Normally a distributed app requires at least one
    registrar per logical host where for that given
    "host space" (aka localhost IPC domain of addresses)
    it is responsible for making all other host (local
    address) bound actors *discoverable* to external
    actor trees running on remote hosts.

    '''
    is_registrar = True

    def is_registry(self) -> bool:
        return self.is_registrar

    def __init__(
        self,
        *args,
        **kwargs,
    ) -> None:

        self._registry: bidict[
            tuple[str, str],
            UnwrappedAddress,
        ] = bidict({})
        self._waiters: dict[
            str,
            # either an event to sync to receiving an
            # actor uid (which is filled in once the actor
            # has sucessfully registered), or that uid
            # after registry is complete.
            list[trio.Event|tuple[str, str]]
        ] = {}

        super().__init__(*args, **kwargs)

    async def find_actor(
        self,
        name: str,

    ) -> UnwrappedAddress|None:

        for uid, addr in self._registry.items():
            if name in uid:
                return addr

        return None

    async def get_registry(
        self

    ) -> dict[str, UnwrappedAddress]:
        '''
        Return current name registry.

        This method is async to allow for cross-actor
        invocation.

        '''
        # NOTE: requires ``strict_map_key=False`` to the
        # msgpack unpacker since we have tuples as keys
        # (note this makes the registrar suscetible to
        # hashdos):
        # https://github.com/msgpack/msgpack-python#major-breaking-changes-in-msgpack-10
        return {
            '.'.join(key): val
            for key, val in self._registry.items()
        }

    async def wait_for_actor(
        self,
        name: str,

    ) -> list[UnwrappedAddress]:
        '''
        Wait for a particular actor to register.

        This is a blocking call if no actor by the
        provided name is currently registered.

        '''
        addrs: list[UnwrappedAddress] = []
        addr: UnwrappedAddress

        mailbox_info: str = (
            'Actor registry contact infos:\n'
        )
        for uid, addr in self._registry.items():
            mailbox_info += (
                f'|_uid: {uid}\n'
                f'|_addr: {addr}\n\n'
            )
            if name == uid[0]:
                addrs.append(addr)

        if not addrs:
            waiter = trio.Event()
            self._waiters.setdefault(
                name, []
            ).append(waiter)
            await waiter.wait()

            for uid in self._waiters[name]:
                if not isinstance(uid, trio.Event):
                    addrs.append(
                        self._registry[uid]
                    )

        log.runtime(mailbox_info)
        return addrs

    async def register_actor(
        self,
        uid: tuple[str, str],
        addr: UnwrappedAddress
    ) -> None:
        uid = name, hash = (
            str(uid[0]),
            str(uid[1]),
        )
        waddr: Address = wrap_address(addr)
        if not waddr.is_valid:
            # should never be 0-dynamic-os-alloc
            await debug.pause()

        # XXX NOTE, value must also be hashable AND since
        # `._registry` is a `bidict` values must be unique;
        # use `.forceput()` to replace any prior (stale)
        # entries that might map a different uid to the same
        # addr (e.g. after an unclean shutdown or
        # actor-restart reusing the same address).
        self._registry.forceput(uid, tuple(addr))

        # pop and signal all waiter events
        events = self._waiters.pop(name, [])
        self._waiters.setdefault(
            name, []
        ).append(uid)
        for event in events:
            if isinstance(event, trio.Event):
                event.set()

    async def unregister_actor(
        self,
        uid: tuple[str, str]

    ) -> None:
        uid = (str(uid[0]), str(uid[1]))
        entry: tuple = self._registry.pop(
            uid, None
        )
        if entry is None:
            log.warning(
                f'Request to de-register'
                f' {uid!r} failed?'
            )

    async def delete_addr(
        self,
        addr: tuple[str, int|str]|list[str|int],
    ) -> tuple[str, str]|None:
        # NOTE: `addr` arrives as a `list` over IPC
        # (msgpack deserializes tuples -> lists) so
        # coerce to `tuple` for the bidict hash lookup.
        uid: tuple[str, str]|None = (
            self._registry.inverse.pop(
                tuple(addr),
                None,
            )
        )
        if uid:
            report: str = (
                'Deleting registry-entry for,\n'
            )
        else:
            report: str = (
                'No registry entry for,\n'
            )

        log.warning(
            report
            +
            f'{addr!r}@{uid!r}'
        )
        return uid


# Backward compat alias
Arbiter = Registrar
