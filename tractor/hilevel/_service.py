# tractor: structured concurrent "actors".
# Copyright 2024-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
Daemon subactor as service(s) management and supervision primitives
and API.

'''
from __future__ import annotations
from contextlib import (
    # asynccontextmanager as acm,
    contextmanager as cm,
)
from collections import defaultdict
from typing import (
    Callable,
    Any,
)

import trio
from trio import TaskStatus
from tractor import (
    ActorNursery,
    current_actor,
    ContextCancelled,
    Context,
    Portal,
)

from ._util import (
    log,  # sub-sys logger
)


# TODO: implement a `@singleton` deco-API for wrapping the below
# factory's impl for general actor-singleton use?
#
# -[ ] go through the options peeps on SO did?
#  * https://stackoverflow.com/questions/6760685/what-is-the-best-way-of-implementing-singleton-in-python
#  * including @mikenerone's answer
#   |_https://stackoverflow.com/questions/6760685/what-is-the-best-way-of-implementing-singleton-in-python/39186313#39186313
#
# -[ ] put it in `tractor.lowlevel._globals` ?
#  * fits with our oustanding actor-local/global feat req?
#   |_ https://github.com/goodboy/tractor/issues/55
#  * how can it relate to the `Actor.lifetime_stack` that was
#    silently patched in?
#   |_ we could implicitly call both of these in the same
#     spot in the runtime using the lifetime stack?
#    - `open_singleton_cm().__exit__()`
#    -`del_singleton()`
#   |_ gives SC fixtue semantics to sync code oriented around
#     sub-process lifetime?
#  * what about with `trio.RunVar`?
#   |_https://trio.readthedocs.io/en/stable/reference-lowlevel.html#trio.lowlevel.RunVar
#    - which we'll need for no-GIL cpython (right?) presuming
#      multiple `trio.run()` calls in process?
#
#
# @singleton
# async def open_service_mngr(
#     **init_kwargs,
# ) -> ServiceMngr:
#     '''
#     Note this function body is invoke IFF no existing singleton instance already
#     exists in this proc's memory.

#     '''
#     # setup
#     yield ServiceMngr(**init_kwargs)
#     # teardown


# a deletion API for explicit instance de-allocation?
# @open_service_mngr.deleter
# def del_service_mngr() -> None:
#     mngr = open_service_mngr._singleton[0]
#     open_service_mngr._singleton[0] = None
#     del mngr



# TODO: singleton factory API instead of a class API
@cm
def open_service_mngr(
    *,
    _singleton: list[ServiceMngr|None] = [None],
    # NOTE; since default values for keyword-args are effectively
    # module-vars/globals as per the note from,
    # https://docs.python.org/3/tutorial/controlflow.html#default-argument-values
    #
    # > "The default value is evaluated only once. This makes
    #   a difference when the default is a mutable object such as
    #   a list, dictionary, or instances of most classes"
    #
    **init_kwargs,

) -> ServiceMngr:
    '''
    Open a multi-subactor-as-service-daemon tree supervisor.

    The delivered `ServiceMngr` is a singleton instance for each
    actor-process and is allocated on first open and never
    de-allocated unless explicitly deleted by al call to
    `del_service_mngr()`.

    '''
    mngr: ServiceMngr|None
    if (mngr := _singleton[0]) is None:
        log.info('Allocating a new service mngr!')
        mngr = _singleton[0] = ServiceMngr(**init_kwargs)
    else:
        log.info(
            'Using extant service mngr!\n\n'
            f'{mngr!r}\n'  # it has a nice `.__repr__()` of services state
        )

    with mngr:
        yield mngr
