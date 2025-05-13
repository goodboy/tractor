# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

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
The fundamental core machinery implementing every "actor"
including the process-local, or "python-interpreter (aka global)
singleton) `Actor` primitive(s) and its internal `trio` machinery
implementing the low level runtime system supporting the
discovery, communication, spawning, supervision and cancellation
of other actors in a hierarchincal process tree.

The runtime's main entry point: `async_main()` opens the top level
supervision and service `trio.Nursery`s which manage the tasks responsible
for running all lower level spawning, supervision and msging layers:

- lowlevel transport-protocol init and persistent connectivity on
  top of `._ipc` primitives; the transport layer.
- bootstrapping of connection/runtime config from the spawning
  parent (actor).
- starting and supervising IPC-channel msg processing loops around
  tranport connections from parent/peer actors in order to deliver
  SC-transitive RPC via scheduling of `trio` tasks.
- registration of newly spawned actors with the discovery sys.

'''
from __future__ import annotations
from contextlib import (
    ExitStack,
)
from functools import partial
import importlib
import importlib.util
import os
from pathlib import Path
from pprint import pformat
import signal
import sys
from typing import (
    Any,
    Callable,
    Type,
    TYPE_CHECKING,
)
import uuid
from types import ModuleType
import warnings

import trio
from trio._core import _run as trio_runtime
from trio import (
    CancelScope,
    Nursery,
    TaskStatus,
)

from tractor.msg import (
    MsgType,
    NamespacePath,
    Stop,
    pretty_struct,
    types as msgtypes,
)
from .ipc import (
    Channel,
    # IPCServer,  # causes cycles atm..
    _server,
)
from ._addr import (
    UnwrappedAddress,
    Address,
    # default_lo_addrs,
    get_address_cls,
    wrap_address,
)
from ._context import (
    mk_context,
    Context,
)
from .log import get_logger
from ._exceptions import (
    ContextCancelled,
    InternalError,
    ModuleNotExposed,
    MsgTypeError,
    unpack_error,
)
from .devx import debug
from ._discovery import get_registry
from ._portal import Portal
from . import _state
from . import _mp_fixup_main
from . import _rpc

if TYPE_CHECKING:
    from ._supervise import ActorNursery
    from trio._channel import MemoryChannelState


log = get_logger('tractor')


def _get_mod_abspath(module: ModuleType) -> Path:
    return Path(module.__file__).absolute()


def get_mod_nsps2fps(mod_ns_paths: list[str]) -> dict[str, str]:
    '''
    Deliver a table of py module namespace-path-`str`s mapped to
    their "physical" `.py` file paths in the file-sys.

    '''
    nsp2fp: dict[str, str] = {}
    for nsp in mod_ns_paths:
        mod: ModuleType = importlib.import_module(nsp)
        nsp2fp[nsp] = str(_get_mod_abspath(mod))

    return nsp2fp


class Actor:
    '''
    The fundamental "runtime" concurrency primitive.

    An "actor" is the combination of a regular Python process
    executing a `trio.run()` task tree, communicating with other
    "actors" through "memory boundary portals": `Portal`, which
    provide a high-level async API around IPC "channels" (`Channel`)
    which themselves encapsulate various (swappable) network
    transport protocols for sending msgs between said memory domains
    (processes, hosts, non-GIL threads).

    Each "actor" is `trio.run()` scheduled "runtime" composed of many
    concurrent tasks in a single thread. The "runtime" tasks conduct
    a slew of low(er) level functions to make it possible for message
    passing between actors as well as the ability to create new
    actors (aka new "runtimes" in new processes which are supervised
    via an "actor-nursery" construct). Each task which sends messages
    to a task in a "peer" actor (not necessarily a parent-child,
    depth hierarchy) is able to do so via an "address", which maps
    IPC connections across memory boundaries, and a task request id
    which allows for per-actor tasks to send and receive messages to
    specific peer-actor tasks with which there is an ongoing RPC/IPC
    dialog.

    '''
    # ugh, we need to get rid of this and replace with a "registry" sys
    # https://github.com/goodboy/tractor/issues/216
    is_arbiter: bool = False

    @property
    def is_registrar(self) -> bool:
        return self.is_arbiter

    msg_buffer_size: int = 2**6

    # nursery placeholders filled in by `async_main()` after fork
    _root_n: Nursery|None = None
    _service_n: Nursery|None = None

    _ipc_server: _server.IPCServer|None = None

    @property
    def ipc_server(self) -> _server.IPCServer:
        '''
        The IPC transport-server for this actor; normally
        a process-singleton.

        '''
        return self._ipc_server

    # Information about `__main__` from parent
    _parent_main_data: dict[str, str]
    _parent_chan_cs: CancelScope|None = None
    _spawn_spec: msgtypes.SpawnSpec|None = None

    # if started on ``asycio`` running ``trio`` in guest mode
    _infected_aio: bool = False

    # TODO: nursery tracking like `trio` does?
    # _ans: dict[
    #     tuple[str, str],
    #     list[ActorNursery],
    # ] = {}

    # Process-global stack closed at end on actor runtime teardown.
    # NOTE: this is currently an undocumented public api.
    lifetime_stack: ExitStack = ExitStack()

    def __init__(
        self,
        name: str,
        uuid: str,
        *,
        enable_modules: list[str] = [],
        loglevel: str|None = None,
        registry_addrs: list[UnwrappedAddress]|None = None,
        spawn_method: str|None = None,

        # TODO: remove!
        arbiter_addr: UnwrappedAddress|None = None,

    ) -> None:
        '''
        This constructor is called in the parent actor **before** the spawning
        phase (aka before a new process is executed).

        '''
        self._aid = msgtypes.Aid(
            name=name,
            uuid=uuid,
            pid=os.getpid(),
        )
        self._task: trio.Task|None = None

        # state
        self._cancel_complete = trio.Event()
        self._cancel_called_by_remote: tuple[str, tuple]|None = None
        self._cancel_called: bool = False

        # retreive and store parent `__main__` data which
        # will be passed to children
        self._parent_main_data = _mp_fixup_main._mp_figure_out_main()

        # TODO? only add this when `is_debug_mode() == True` no?
        # always include debugging tools module
        if _state.is_root_process():
            enable_modules.append('tractor.devx.debug')

        self.enable_modules: dict[str, str] = get_mod_nsps2fps(
            mod_ns_paths=enable_modules,
        )

        self._mods: dict[str, ModuleType] = {}
        self.loglevel: str = loglevel

        if arbiter_addr is not None:
            warnings.warn(
                '`Actor(arbiter_addr=<blah>)` is now deprecated.\n'
                'Use `registry_addrs: list[tuple]` instead.',
                DeprecationWarning,
                stacklevel=2,
            )
            registry_addrs: list[UnwrappedAddress] = [arbiter_addr]

        # marked by the process spawning backend at startup
        # will be None for the parent most process started manually
        # by the user (currently called the "arbiter")
        self._spawn_method: str = spawn_method

        # RPC state
        self._ongoing_rpc_tasks = trio.Event()
        self._ongoing_rpc_tasks.set()
        self._rpc_tasks: dict[
            tuple[Channel, str],  # (chan, cid)
            tuple[Context, Callable, trio.Event]  # (ctx=>, fn(), done?)
        ] = {}

        # map {actor uids -> Context}
        self._contexts: dict[
            tuple[
                tuple[str, str],  # .uid
                str,  # .cid
                str,  # .side
            ],
            Context
        ] = {}

        self._parent_chan: Channel|None = None
        self._forkserver_info: tuple|None = None

        # track each child/sub-actor in it's locally
        # supervising nursery
        self._actoruid2nursery: dict[
            tuple[str, str],  # sub-`Actor.uid`
            ActorNursery|None,
        ] = {}

        # when provided, init the registry addresses property from
        # input via the validator.
        self._reg_addrs: list[UnwrappedAddress] = []
        if registry_addrs:
            self.reg_addrs: list[UnwrappedAddress] = registry_addrs
            _state._runtime_vars['_registry_addrs'] = registry_addrs

    @property
    def aid(self) -> msgtypes.Aid:
        '''
        This process-singleton-actor's "unique actor ID" in struct form.

        See the `tractor.msg.Aid` struct for details.

        '''
        return self._aid

    @property
    def name(self) -> str:
        return self._aid.name

    @property
    def uid(self) -> tuple[str, str]:
        '''
        This process-singleton's "unique (cross-host) ID".

        Delivered from the `.Aid.name/.uuid` fields as a `tuple` pair
        and should be multi-host unique despite a large distributed
        process plane.

        '''
        msg: str = (
            f'`{type(self).__name__}.uid` is now deprecated.\n'
            'Use the new `.aid: tractor.msg.Aid` (struct) instead '
            'which also provides additional named (optional) fields '
            'beyond just the `.name` and `.uuid`.'
        )
        warnings.warn(
            msg,
            DeprecationWarning,
            stacklevel=2,
        )
        return (
            self._aid.name,
            self._aid.uuid,
        )

    @property
    def pid(self) -> int:
        return self._aid.pid

    def pformat(self) -> str:
        ds: str = '='
        parent_uid: tuple|None = None
        if rent_chan := self._parent_chan:
            parent_uid = rent_chan.uid

        peers: list = []
        server: _server.IPCServer = self.ipc_server
        if server:
            peers: list[tuple] = list(server._peer_connected)

        fmtstr: str = (
            f' |_id: {self.aid!r}\n'
            # f"   aid{ds}{self.aid!r}\n"
            f"   parent{ds}{parent_uid}\n"
            f'\n'
            f' |_ipc: {len(peers)!r} connected peers\n'
            f"   peers{ds}{peers!r}\n"
            f"   ipc_server{ds}{self._ipc_server}\n"
            f'\n'
            f' |_rpc: {len(self._rpc_tasks)} tasks\n'
            f"   ctxs{ds}{len(self._contexts)}\n"
            f'\n'
            f' |_runtime: ._task{ds}{self._task!r}\n'
            f'   _spawn_method{ds}{self._spawn_method}\n'
            f'   _actoruid2nursery{ds}{self._actoruid2nursery}\n'
            f'   _forkserver_info{ds}{self._forkserver_info}\n'
            f'\n'
            f' |_state: "TODO: .repr_state()"\n'
            f'   _cancel_complete{ds}{self._cancel_complete}\n'
            f'   _cancel_called_by_remote{ds}{self._cancel_called_by_remote}\n'
            f'   _cancel_called{ds}{self._cancel_called}\n'
        )
        return (
            '<Actor(\n'
            +
            fmtstr
            +
            ')>\n'
        )

    __repr__ = pformat

    @property
    def reg_addrs(self) -> list[UnwrappedAddress]:
        '''
        List of (socket) addresses for all known (and contactable)
        registry actors.

        '''
        return self._reg_addrs

    @reg_addrs.setter
    def reg_addrs(
        self,
        addrs: list[UnwrappedAddress],
    ) -> None:
        if not addrs:
            log.warning(
                'Empty registry address list is invalid:\n'
                f'{addrs}'
            )
            return

        self._reg_addrs = addrs

    def load_modules(
        self,
    ) -> None:
        '''
        Load explicitly enabled python modules from local fs after
        process spawn.

        Since this actor may be spawned on a different machine from
        the original nursery we need to try and load the local module
        code manually (presuming it exists).

        '''
        try:
            if self._spawn_method == 'trio':
                parent_data = self._parent_main_data
                if 'init_main_from_name' in parent_data:
                    _mp_fixup_main._fixup_main_from_name(
                        parent_data['init_main_from_name'])
                elif 'init_main_from_path' in parent_data:
                    _mp_fixup_main._fixup_main_from_path(
                        parent_data['init_main_from_path'])

            status: str = 'Attempting to import enabled modules:\n'

            modpath: str
            filepath: str
            for modpath, filepath in self.enable_modules.items():
                # XXX append the allowed module to the python path which
                # should allow for relative (at least downward) imports.
                sys.path.append(os.path.dirname(filepath))
                status += (
                    f'|_{modpath!r} -> {filepath!r}\n'
                )
                mod: ModuleType = importlib.import_module(modpath)
                self._mods[modpath] = mod
                if modpath == '__main__':
                    self._mods['__mp_main__'] = mod

            log.runtime(status)

        except ModuleNotFoundError:
            # it is expected the corresponding `ModuleNotExposed` error
            # will be raised later
            log.error(
                f"Failed to import {modpath} in {self.name}"
            )
            raise

    def _get_rpc_func(self, ns, funcname):
        '''
        Try to lookup and return a target RPC func from the
        post-fork enabled module set.

        '''
        try:
            return getattr(self._mods[ns], funcname)
        except KeyError as err:
            mne = ModuleNotExposed(*err.args)

            if ns == '__main__':
                modpath = '__name__'
            else:
                modpath = f"'{ns}'"

            msg = (
                "\n\nMake sure you exposed the target module, `{ns}`, "
                "using:\n"
                "ActorNursery.start_actor(<name>, enable_modules=[{mod}])"
            ).format(
                ns=ns,
                mod=modpath,
            )

            mne.msg += msg

            raise mne

    # TODO: rename to `._deliver_payload()` since this handles
    # more then just `result` msgs now obvi XD
    async def _deliver_ctx_payload(
        self,
        chan: Channel,
        cid: str,
        msg: MsgType|MsgTypeError,

    ) -> None|bool:
        '''
        Push an RPC msg-payload to the local consumer peer-task's
        queue.

        '''
        uid: tuple[str, str] = chan.uid
        assert uid, f"`chan.uid` can't be {uid}"
        try:
            ctx: Context = self._contexts[(
                uid,
                cid,

                # TODO: how to determine this tho?
                # side,
            )]
        except KeyError:
            report: str = (
                'Ignoring invalid IPC msg!?\n'
                f'Ctx seems to not/no-longer exist??\n'
                f'\n'
                f'<=? {uid}\n'
                f'  |_{pretty_struct.pformat(msg)}\n'
            )
            match msg:
                case Stop():
                    log.runtime(report)
                case _:
                    log.warning(report)

            return

        # if isinstance(msg, MsgTypeError):
        #     return await ctx._deliver_bad_msg()

        return await ctx._deliver_msg(msg)

    def get_context(
        self,
        chan: Channel,
        cid: str,
        nsf: NamespacePath,

        # TODO: support lookup by `Context.side: str` ?
        # -> would allow making a self-context which might have
        # certain special use cases where RPC isolation is wanted
        # between 2 tasks running in the same process?
        # => prolly needs some deeper though on the real use cases
        # and whether or not such things should be better
        # implemented using a `TaskManager` style nursery..
        #
        # side: str|None = None,

        msg_buffer_size: int|None = None,
        allow_overruns: bool = False,

    ) -> Context:
        '''
        Look-up (existing) or create a new
        inter-actor-SC-linked task "context" (a `Context`) which
        encapsulates the local RPC task's execution enviroment
        around `Channel` relayed msg handling including,

        - a dedicated `trio` cancel scope (`Context._scope`),
        - a pair of IPC-msg-relay "feeder" mem-channels
          (`Context._recv/send_chan`),
        - and a "context id" (cid) unique to the task-pair
          msging session's lifetime.

        '''
        actor_uid = chan.uid
        assert actor_uid
        try:
            ctx = self._contexts[(
                actor_uid,
                cid,
                # side,
            )]
            log.debug(
                f'Retreived cached IPC ctx for\n'
                f'peer: {chan.uid}\n'
                f'cid:{cid}\n'
            )
            ctx._allow_overruns: bool = allow_overruns

            # adjust buffer size if specified
            state: MemoryChannelState  = ctx._send_chan._state  # type: ignore
            if (
                msg_buffer_size
                and
                state.max_buffer_size != msg_buffer_size
            ):
                state.max_buffer_size = msg_buffer_size

        except KeyError:
            log.debug(
                f'Allocate new IPC ctx for\n'
                f'peer: {chan.uid}\n'
                f'cid: {cid}\n'
            )
            ctx = mk_context(
                chan,
                cid,
                nsf=nsf,
                msg_buffer_size=msg_buffer_size or self.msg_buffer_size,
                _allow_overruns=allow_overruns,
            )
            self._contexts[(
                actor_uid,
                cid,
                # side,
            )] = ctx

        return ctx

    async def start_remote_task(
        self,
        chan: Channel,
        nsf: NamespacePath,
        kwargs: dict,

        # determines `Context.side: str`
        portal: Portal|None = None,

        # IPC channel config
        msg_buffer_size: int|None = None,
        allow_overruns: bool = False,
        load_nsf: bool = False,
        ack_timeout: float = float('inf'),

    ) -> Context:
        '''
        Send a `'cmd'` msg to a remote actor, which requests the
        start and schedule of a remote task-as-function's
        entrypoint.

        Synchronously validates the endpoint type and returns
        a (caller side) `Context` that can be used to accept
        delivery of msg payloads from the local runtime's
        processing loop: `._rpc.process_messages()`.

        '''
        cid: str = str(uuid.uuid4())
        assert chan.uid
        ctx = self.get_context(
            chan=chan,
            cid=cid,
            nsf=nsf,

            # side='caller',
            msg_buffer_size=msg_buffer_size,
            allow_overruns=allow_overruns,
        )
        ctx._portal = portal

        if (
            'self' in nsf
            or
            not load_nsf
        ):
            ns, _, func = nsf.partition(':')
        else:
            # TODO: pass nsf directly over wire!
            # -[ ] but, how to do `self:<Actor.meth>`??
            ns, func = nsf.to_tuple()

        msg = msgtypes.Start(
            ns=ns,
            func=func,
            kwargs=kwargs,
            uid=self.uid,
            cid=cid,
        )
        log.runtime(
            'Sending RPC `Start`\n\n'
            f'=> peer: {chan.uid}\n'
            f'  |_ {ns}.{func}({kwargs})\n\n'

            f'{pretty_struct.pformat(msg)}'
        )
        await chan.send(msg)

        # NOTE wait on first `StartAck` response msg and validate;
        # this should be immediate and does not (yet) wait for the
        # remote child task to sync via `Context.started()`.
        with trio.fail_after(ack_timeout):
            first_msg: msgtypes.StartAck = await ctx._rx_chan.receive()
        try:
            functype: str = first_msg.functype
        except AttributeError:
            raise unpack_error(first_msg, chan)

        if functype not in (
            'asyncfunc',
            'asyncgen',
            'context',
        ):
            raise ValueError(
                f'Invalid `StartAck.functype: str = {first_msg!r}` ??'
            )

        ctx._remote_func_type = functype
        return ctx

    async def _from_parent(
        self,
        parent_addr: UnwrappedAddress|None,

    ) -> tuple[
        Channel,
        list[UnwrappedAddress]|None,
        list[str]|None,  # preferred tpts
    ]:
        '''
        Bootstrap this local actor's runtime config from its parent by
        connecting back via the IPC transport, handshaking and then 
        `Channel.recv()`-ing seeded data.

        '''
        try:
            # Connect back to the parent actor and conduct initial
            # handshake. From this point on if we error, we
            # attempt to ship the exception back to the parent.
            chan = await Channel.from_addr(
                addr=wrap_address(parent_addr)
            )
            assert isinstance(chan, Channel)

            # init handshake: swap actor-IDs.
            await chan._do_handshake(aid=self.aid)

            accept_addrs: list[UnwrappedAddress]|None = None

            if self._spawn_method == "trio":

                # Receive post-spawn runtime state from our parent.
                spawnspec: msgtypes.SpawnSpec = await chan.recv()
                match spawnspec:
                    case MsgTypeError():
                        raise spawnspec
                    case msgtypes.SpawnSpec():
                        self._spawn_spec = spawnspec
                        log.runtime(
                            'Received runtime spec from parent:\n\n'

                            # TODO: eventually all these msgs as
                            # `msgspec.Struct` with a special mode that
                            # pformats them in multi-line mode, BUT only
                            # if "trace"/"util" mode is enabled?
                            f'{pretty_struct.pformat(spawnspec)}\n'
                        )

                    case _:
                        raise InternalError(
                            f'Received invalid non-`SpawnSpec` payload !?\n'
                            f'{spawnspec}\n'
                        )
                # ^^XXX TODO XXX^^^
                # when the `SpawnSpec` fails to decode the above will
                # raise a `MsgTypeError` which if we do NOT ALSO
                # RAISE it will tried to be pprinted in the
                # log.runtime() below..
                #
                # SO we gotta look at how other `chan.recv()` calls
                # are wrapped and do the same for this spec receive!
                # -[ ] see `._rpc` likely has the answer?

                # ^^^XXX NOTE XXX^^^, can't be called here!
                #
                # breakpoint()
                # import pdbp; pdbp.set_trace()
                #
                # => bc we haven't yet received the
                # `spawnspec._runtime_vars` which contains
                # `debug_mode: bool`..

                # `SpawnSpec.bind_addrs`
                #  ---------------------
                accept_addrs: list[UnwrappedAddress] = spawnspec.bind_addrs

                # `SpawnSpec._runtime_vars`
                # -------------------------
                # => update process-wide globals
                # TODO! -[ ] another `Struct` for rtvs..
                rvs: dict[str, Any] = spawnspec._runtime_vars
                if rvs['_debug_mode']:
                    from .devx import (
                        enable_stack_on_sig,
                        maybe_init_greenback,
                    )
                    try:
                        # TODO: maybe return some status msgs upward
                        # to that we can emit them in `con_status`
                        # instead?
                        log.devx(
                            'Enabling `stackscope` traces on SIGUSR1'
                        )
                        enable_stack_on_sig()

                    except ImportError:
                        log.warning(
                            '`stackscope` not installed for use in debug mode!'
                        )

                    if rvs.get('use_greenback', False):
                        maybe_mod: ModuleType|None = await maybe_init_greenback()
                        if maybe_mod:
                            log.devx(
                                'Activated `greenback` '
                                'for `tractor.pause_from_sync()` support!'
                            )
                        else:
                            rvs['use_greenback'] = False
                            log.warning(
                                '`greenback` not installed for use in debug mode!\n'
                                '`tractor.pause_from_sync()` not available!'
                            )

                # XXX ensure the "infected `asyncio` mode" setting
                # passed down from our spawning parent is consistent
                # with `trio`-runtime initialization:
                # - during sub-proc boot, the entrypoint func
                #   (`._entry.<spawn_backend>_main()`) should set
                #   `._infected_aio = True` before calling
                #   `run_as_asyncio_guest()`,
                # - the value of `infect_asyncio: bool = True` as
                #   passed to `ActorNursery.start_actor()` must be
                #   the same as `_runtime_vars['_is_infected_aio']`
                if (
                    (aio_rtv := rvs['_is_infected_aio'])
                    !=
                    (aio_attr := self._infected_aio)
                ):
                    raise InternalError(
                        'Parent sent runtime-vars that mismatch for the '
                        '"infected `asyncio` mode" settings ?!?\n\n'

                        f'rvs["_is_infected_aio"] = {aio_rtv}\n'
                        f'self._infected_aio = {aio_attr}\n'
                    )
                if aio_rtv:
                    assert (
                        trio_runtime.GLOBAL_RUN_CONTEXT.runner.is_guest
                        # and
                        # ^TODO^ possibly add a `sniffio` or
                        # `trio` pub-API for `is_guest_mode()`?
                    )

                rvs['_is_root'] = False  # obvi XD

                _state._runtime_vars.update(rvs)

                # `SpawnSpec.reg_addrs`
                # ---------------------
                # => update parent provided registrar contact info
                #
                self.reg_addrs = [
                    # TODO: we don't really NEED these as tuples?
                    # so we can probably drop this casting since
                    # apparently in python lists are "more
                    # efficient"?
                    tuple(val)
                    for val in spawnspec.reg_addrs
                ]

                # `SpawnSpec.enable_modules`
                # ---------------------
                # => extend RPC-python-module (capabilities) with
                #   those permitted by parent.
                #
                # NOTE, only the root actor should have
                # a pre-permitted entry for `.devx.debug._tty_lock`.
                assert not self.enable_modules
                self.enable_modules.update(
                    spawnspec.enable_modules
                )

                self._parent_main_data = spawnspec._parent_main_data
                # XXX QUESTION(s)^^^
                # -[ ] already set in `.__init__()` right, but how is
                #      it diff from this blatant parent copy?
                #    -[ ] do we need/want the .__init__() value in
                #       just the root case orr?

            return (
                chan,
                accept_addrs,
                _state._runtime_vars['_enable_tpts']
            )

        # failed to connect back?
        except (
            OSError,
            ConnectionError,
        ):
            log.warning(
                f'Failed to connect to spawning parent actor!?\n'
                f'\n'
                f'x=> {parent_addr}\n'
                f'  |_{self}\n\n'
            )
            await self.cancel(req_chan=None)  # self cancel
            raise

    def cancel_soon(self) -> None:
        '''
        Cancel this actor asap; can be called from a sync context.

        Schedules runtime cancellation via `Actor.cancel()` inside
        the RPC service nursery.

        '''
        assert self._service_n
        self._service_n.start_soon(
            self.cancel,
            None,  # self cancel all rpc tasks
        )

    async def cancel(
        self,

        # chan whose lifetime limits the lifetime of its remotely
        # requested and locally spawned RPC tasks - similar to the
        # supervision semantics of a nursery wherein the actual
        # implementation does start all such tasks in a sub-nursery.
        req_chan: Channel|None,

    ) -> bool:
        '''
        Cancel this actor's runtime, eventually resulting in
        termination of its containing OS process.

        The ideal "deterministic" teardown sequence in order is:
         - cancel all ongoing rpc tasks by cancel scope.
         - cancel the channel server to prevent new inbound
           connections.
         - cancel the "service" nursery reponsible for
           spawning new rpc tasks.
         - return control the parent channel message loop.

        '''
        (
            requesting_uid,
            requester_type,
            req_chan,
            log_meth,
        ) = (
            req_chan.uid,
            'peer',
            req_chan,
            log.cancel,

        ) if req_chan else (

            # a self cancel of ALL rpc tasks
            self.uid,
            'self',
            self,
            log.runtime,
        )
        # TODO: just use the new `Context.repr_rpc: str` (and
        # other) repr fields instead of doing this all manual..
        msg: str = (
            f'Actor-runtime cancel request from {requester_type}\n\n'
            f'<=c) {requesting_uid}\n'
            f'  |_{self}\n'
            f'\n'
        )

        # TODO: what happens here when we self-cancel tho?
        self._cancel_called_by_remote: tuple = requesting_uid
        self._cancel_called = True

        # cancel all ongoing rpc tasks
        with CancelScope(shield=True):

            # kill any debugger request task to avoid deadlock
            # with the root actor in this tree
            debug_req = debug.DebugStatus
            lock_req_ctx: Context = debug_req.req_ctx
            if (
                lock_req_ctx
                and
                lock_req_ctx.has_outcome
            ):
                msg += (
                    f'\n'
                    f'-> Cancelling active debugger request..\n'
                    f'|_{debug.Lock.repr()}\n\n'
                    f'|_{lock_req_ctx}\n\n'
                )
                # lock_req_ctx._scope.cancel()
                # TODO: wrap this in a method-API..
                debug_req.req_cs.cancel()
                # if lock_req_ctx:

            # self-cancel **all** ongoing RPC tasks
            await self.cancel_rpc_tasks(
                req_uid=requesting_uid,
                parent_chan=None,
            )

            # stop channel server
            if ipc_server := self.ipc_server:
                ipc_server.cancel()
                await ipc_server.wait_for_shutdown()

            # cancel all rpc tasks permanently
            if self._service_n:
                self._service_n.cancel_scope.cancel()

        log_meth(msg)
        self._cancel_complete.set()
        return True

    # XXX: hard kill logic if needed?
    # def _hard_mofo_kill(self):
    #     # If we're the root actor or zombied kill everything
    #     if self._parent_chan is None:  # TODO: more robust check
    #         root = trio.lowlevel.current_root_task()
    #         for n in root.child_nurseries:
    #             n.cancel_scope.cancel()

    async def _cancel_task(
        self,
        cid: str,
        parent_chan: Channel,
        requesting_uid: tuple[str, str]|None,

        ipc_msg: dict|None|bool = False,

    ) -> bool:
        '''
        Cancel a local (RPC) task by context-id/channel by calling
        `trio.CancelScope.cancel()` on it's surrounding cancel
        scope.

        '''

        # this ctx based lookup ensures the requested task to be
        # cancelled was indeed spawned by a request from its
        # parent (or some grandparent's) channel
        ctx: Context
        func: Callable
        is_complete: trio.Event
        try:
            (
                ctx,
                func,
                is_complete,
            ) = self._rpc_tasks[(
                parent_chan,
                cid,
            )]
            scope: CancelScope = ctx._scope

        except KeyError:
            # NOTE: during msging race conditions this will often
            # emit, some examples:
            # - child returns a result before cancel-msg/ctxc-raised
            # - child self raises ctxc before parent send request,
            # - child errors prior to cancel req.
            log.runtime(
                'Cancel request for invalid RPC task.\n'
                'The task likely already completed or was never started!\n\n'
                f'<= canceller: {requesting_uid}\n'
                f'=> {cid}@{parent_chan.uid}\n'
                f'  |_{parent_chan}\n'
            )
            return True

        log.cancel(
            'Rxed cancel request for RPC task\n'
            f'<=c) {requesting_uid}\n'
            f' |_{ctx._task}\n'
            f'    >> {ctx.repr_rpc}\n'
            # f'=> {ctx._task}\n'
            # f'  >> Actor._cancel_task() => {ctx._task}\n'
            # f'  |_ {ctx._task}\n\n'

            # TODO: better ascii repr for "supervisor" like
            # a nursery or context scope?
            # f'=> {parent_chan}\n'
            # f'   |_{ctx._task}\n'
            # TODO: simplified `Context.__repr__()` fields output
            # shows only application state-related stuff like,
            # - ._stream
            # - .closed
            # - .started_called
            # - .. etc.
            # f'     >> {ctx.repr_rpc}\n'
            # f'  |_ctx: {cid}\n'
            # f'    >> {ctx._nsf}()\n'
        )
        if (
            ctx._canceller is None
            and requesting_uid
        ):
            ctx._canceller: tuple = requesting_uid

        # TODO: pack the RPC `{'cmd': <blah>}` msg into a ctxc and
        # then raise and pack it here?
        if (
            ipc_msg
            and ctx._cancel_msg is None
        ):
            # assign RPC msg directly from the loop which usually
            # the case with `ctx.cancel()` on the other side.
            ctx._cancel_msg = ipc_msg

        # don't allow cancelling this function mid-execution
        # (is this necessary?)
        if func is self._cancel_task:
            log.error('Do not cancel a cancel!?')
            return True

        # TODO: shouldn't we eventually be calling ``Context.cancel()``
        # directly here instead (since that method can handle both
        # side's calls into it?
        # await ctx.cancel()
        scope.cancel()

        # wait for _invoke to mark the task complete
        flow_info: str = (
            f'<= canceller: {requesting_uid}\n'
            f'=> ipc-parent: {parent_chan}\n'
            f'|_{ctx}\n'
        )
        log.runtime(
            'Waiting on RPC task to cancel\n\n'
            f'{flow_info}'
        )
        await is_complete.wait()
        log.runtime(
            f'Sucessfully cancelled RPC task\n\n'
            f'{flow_info}'
        )
        return True

    async def cancel_rpc_tasks(
        self,
        req_uid: tuple[str, str],

        # NOTE: when None is passed we cancel **all** rpc
        # tasks running in this actor!
        parent_chan: Channel|None,

    ) -> None:
        '''
        Cancel all ongoing RPC tasks owned/spawned for a given
        `parent_chan: Channel` or simply all tasks (inside
        `._service_n`) when `parent_chan=None`.

        '''
        tasks: dict = self._rpc_tasks
        if not tasks:
            log.runtime(
                'Actor has no cancellable RPC tasks?\n'
                f'<= canceller: {req_uid}\n'
            )
            return

        # TODO: seriously factor this into some helper funcs XD
        tasks_str: str = ''
        for (ctx, func, _) in tasks.values():

            # TODO: std repr of all primitives in
            # a hierarchical tree format, since we can!!
            # like => repr for funcs/addrs/msg-typing:
            #
            # -[ ] use a proper utf8 "arm" like
            #     `stackscope` has!
            # -[ ] for typed msging, show the
            #      py-type-annot style?
            #  - maybe auto-gen via `inspect` / `typing` type-sig:
            #   https://stackoverflow.com/a/57110117
            #   => see ex. code pasted into `.msg.types`
            #
            # -[ ] proper .maddr() for IPC primitives?
            #   - `Channel.maddr() -> str:` obvi!
            #   - `Context.maddr() -> str:`
            tasks_str += (
                f' |_@ /ipv4/tcp/cid="{ctx.cid[-16:]} .."\n'
                f'   |>> {ctx._nsf}() -> dict:\n'
            )

        descr: str = (
            'all' if not parent_chan
            else
            "IPC channel's "
        )
        rent_chan_repr: str = (
            f' |_{parent_chan}\n\n'
            if parent_chan
            else ''
        )
        log.cancel(
            f'Cancelling {descr} RPC tasks\n\n'
            f'<=c) {req_uid} [canceller]\n'
            f'{rent_chan_repr}'
            f'c)=> {self.uid} [cancellee]\n'
            f'  |_{self} [with {len(tasks)} tasks]\n'
            # f'  |_tasks: {len(tasks)}\n'
            # f'{tasks_str}'
        )
        for (
            (task_caller_chan, cid),
            (ctx, func, is_complete),
        ) in tasks.copy().items():

            if (
                # maybe filter to specific IPC channel?
                (parent_chan
                 and
                 task_caller_chan != parent_chan)

                # never "cancel-a-cancel" XD
                or (func == self._cancel_task)
            ):
                continue

            # TODO: this maybe block on the task cancellation
            # and so should really done in a nursery batch?
            await self._cancel_task(
                cid,
                task_caller_chan,
                requesting_uid=req_uid,
            )

        if tasks:
            log.cancel(
                'Waiting for remaining rpc tasks to complete\n'
                f'|_{tasks_str}'
            )
        await self._ongoing_rpc_tasks.wait()

    @property
    def accept_addrs(self) -> list[UnwrappedAddress]:
        '''
        All addresses to which the transport-channel server binds
        and listens for new connections.

        '''
        return self._ipc_server.accept_addrs

    @property
    def accept_addr(self) -> UnwrappedAddress:
        '''
        Primary address to which the IPC transport server is
        bound and listening for new connections.

        '''
        return self.accept_addrs[0]

    def get_parent(self) -> Portal:
        '''
        Return a `Portal` to our parent.

        '''
        assert self._parent_chan, "No parent channel for this actor?"
        return Portal(self._parent_chan)

    def get_chans(
        self,
        uid: tuple[str, str],

    ) -> list[Channel]:
        '''
        Return all IPC channels to the actor with provided `uid`.

        '''
        return self._peers[uid]

    def is_infected_aio(self) -> bool:
        '''
        If `True`, this actor is running `trio` in guest mode on
        the `asyncio` event loop and thus can use the APIs in
        `.to_asyncio` to coordinate tasks running in each
        framework but within the same actor runtime.

        '''
        return self._infected_aio


async def async_main(
    actor: Actor,
    accept_addrs: UnwrappedAddress|None = None,

    # XXX: currently ``parent_addr`` is only needed for the
    # ``multiprocessing`` backend (which pickles state sent to
    # the child instead of relaying it over the connect-back
    # channel). Once that backend is removed we can likely just
    # change this to a simple ``is_subactor: bool`` which will
    # be False when running as root actor and True when as
    # a subactor.
    parent_addr: UnwrappedAddress|None = None,
    task_status: TaskStatus[None] = trio.TASK_STATUS_IGNORED,

) -> None:
    '''
    Main `Actor` runtime entrypoint; start the transport-specific
    IPC channel server, (maybe) connect back to parent (to receive
    additional config), startup all core `trio` machinery for
    delivering RPCs, register with the discovery system.

    The "root" (or "top-level") and "service" `trio.Nursery`s are
    opened here and when cancelled/terminated effectively shutdown
    the actor's "runtime" and all thus all ongoing RPC tasks.

    '''
    # XXX NOTE, `_state._current_actor` **must** be set prior to
    # calling this core runtime entrypoint!
    assert actor is _state.current_actor()

    actor._task: trio.Task = trio.lowlevel.current_task()

    # attempt to retreive ``trio``'s sigint handler and stash it
    # on our debugger state.
    debug.DebugStatus._trio_handler = signal.getsignal(signal.SIGINT)

    is_registered: bool = False
    try:

        # establish primary connection with immediate parent
        actor._parent_chan: Channel|None = None

        if parent_addr is not None:
            (
                actor._parent_chan,
                set_accept_addr_says_rent,
                maybe_preferred_transports_says_rent,
            ) = await actor._from_parent(parent_addr)

            accept_addrs: list[UnwrappedAddress] = []
            # either it's passed in because we're not a child or
            # because we're running in mp mode
            if (
                set_accept_addr_says_rent
                and
                set_accept_addr_says_rent is not None
            ):
                accept_addrs = set_accept_addr_says_rent
            else:
                enable_transports: list[str] = (
                    maybe_preferred_transports_says_rent
                    or
                    [_state._def_tpt_proto]
                )
                for transport_key in enable_transports:
                    transport_cls: Type[Address] = get_address_cls(
                        transport_key
                    )
                    addr: Address = transport_cls.get_random()
                    accept_addrs.append(addr.unwrap())

        assert accept_addrs
        # The "root" nursery ensures the channel with the immediate
        # parent is kept alive as a resilient service until
        # cancellation steps have (mostly) occurred in
        # a deterministic way.
        async with trio.open_nursery(
            strict_exception_groups=False,
        ) as root_nursery:
            actor._root_n = root_nursery
            assert actor._root_n

            ipc_server: _server.IPCServer
            async with (
                trio.open_nursery(
                    strict_exception_groups=False,
                ) as service_nursery,

                _server.open_ipc_server(
                    parent_tn=service_nursery,
                    stream_handler_tn=service_nursery,
                ) as ipc_server,
                # ) as actor._ipc_server,
                # ^TODO? prettier?

            ):
                # This nursery is used to handle all inbound
                # connections to us such that if the TCP server
                # is killed, connections can continue to process
                # in the background until this nursery is cancelled.
                actor._service_n = service_nursery
                actor._ipc_server = ipc_server
                assert (
                    actor._service_n
                    and (
                        actor._service_n
                        is
                        actor._ipc_server._parent_tn
                        is
                        ipc_server._stream_handler_tn
                    )
                )

                # load exposed/allowed RPC modules
                # XXX: do this **after** establishing a channel to the parent
                # but **before** starting the message loop for that channel
                # such that import errors are properly propagated upwards
                actor.load_modules()

                # XXX TODO XXX: figuring out debugging of this
                # would somemwhat guarantee "self-hosted" runtime
                # debugging (since it hits all the ede cases?)
                #
                # `tractor.pause()` right?
                # try:
                #     actor.load_modules()
                # except ModuleNotFoundError as err:
                #     debug.pause_from_sync()
                #     import pdbp; pdbp.set_trace()
                #     raise

                # Startup up the transport(-channel) server with,
                # - subactor: the bind address is sent by our parent
                #   over our established channel
                # - root actor: the ``accept_addr`` passed to this method

                # TODO: why is this not with the root nursery?
                try:
                    log.runtime(
                        'Booting IPC server'
                    )
                    eps: list = await ipc_server.listen_on(
                        accept_addrs=accept_addrs,
                        stream_handler_nursery=service_nursery,
                    )
                    log.runtime(
                        f'Booted IPC server\n'
                        f'{ipc_server}\n'
                    )
                    assert (
                        (eps[0].listen_tn)
                        is not service_nursery
                    )

                except OSError as oserr:
                    # NOTE: always allow runtime hackers to debug
                    # tranport address bind errors - normally it's
                    # something silly like the wrong socket-address
                    # passed via a config or CLI Bo
                    entered_debug: bool = await debug._maybe_enter_pm(
                        oserr,
                    )
                    if not entered_debug:
                        log.exception('Failed to init IPC server !?\n')
                    else:
                        log.runtime('Exited debug REPL..')

                    raise

                # TODO, just read direct from ipc_server?
                accept_addrs: list[UnwrappedAddress] = actor.accept_addrs

                # NOTE: only set the loopback addr for the 
                # process-tree-global "root" mailbox since
                # all sub-actors should be able to speak to
                # their root actor over that channel.
                if _state._runtime_vars['_is_root']:
                    raddrs: list[Address] = _state._runtime_vars['_root_addrs']
                    for addr in accept_addrs:
                        waddr: Address = wrap_address(addr)
                        raddrs.append(addr)
                    else:
                        _state._runtime_vars['_root_mailbox'] = raddrs[0]

                # Register with the arbiter if we're told its addr
                log.runtime(
                    f'Registering `{actor.name}` => {pformat(accept_addrs)}\n'
                    # ^-TODO-^ we should instead show the maddr here^^
                )

                # TODO: ideally we don't fan out to all registrars
                # if addresses point to the same actor..
                # So we need a way to detect that? maybe iterate
                # only on unique actor uids?
                for addr in actor.reg_addrs:
                    try:
                        waddr = wrap_address(addr)
                        assert waddr.is_valid
                    except AssertionError:
                        await debug.pause()

                    async with get_registry(addr) as reg_portal:
                        for accept_addr in accept_addrs:
                            accept_addr = wrap_address(accept_addr)

                            if not accept_addr.is_valid:
                                breakpoint()

                            await reg_portal.run_from_ns(
                                'self',
                                'register_actor',
                                uid=actor.uid,
                                addr=accept_addr.unwrap(),
                            )

                    is_registered: bool = True

                # init steps complete
                task_status.started()

                # Begin handling our new connection back to our
                # parent. This is done last since we don't want to
                # start processing parent requests until our channel
                # server is 100% up and running.
                if actor._parent_chan:
                    await root_nursery.start(
                        partial(
                            _rpc.process_messages,
                            chan=actor._parent_chan,
                            shield=True,
                        )
                    )
                log.runtime(
                    'Actor runtime is up!'
                    # 'Blocking on service nursery to exit..\n'
                )
            log.runtime(
                "Service nursery complete\n"
                "Waiting on root nursery to complete"
            )

        # Blocks here as expected until the root nursery is
        # killed (i.e. this actor is cancelled or signalled by the parent)
    except Exception as internal_err:
        if not is_registered:
            err_report: str = (
                '\n'
                "Actor runtime (internally) failed BEFORE contacting the registry?\n"
                f'registrars -> {actor.reg_addrs} ?!?!\n\n'

                '^^^ THIS IS PROBABLY AN INTERNAL `tractor` BUG! ^^^\n\n'
                '\t>> CALMLY CANCEL YOUR CHILDREN AND CALL YOUR PARENTS <<\n\n'

                '\tIf this is a sub-actor hopefully its parent will keep running '
                'and cancel/reap this sub-process..\n'
                '(well, presuming this error was propagated upward)\n\n'

                '\t---------------------------------------------\n'
                '\tPLEASE REPORT THIS TRACEBACK IN A BUG REPORT @ '  # oneline
                'https://github.com/goodboy/tractor/issues\n'
                '\t---------------------------------------------\n'
            )

            # TODO: I guess we could try to connect back
            # to the parent through a channel and engage a debugger
            # once we have that all working with std streams locking?
            log.exception(err_report)

        if actor._parent_chan:
            await _rpc.try_ship_error_to_remote(
                actor._parent_chan,
                internal_err,
            )

        # always!
        match internal_err:
            case ContextCancelled():
                log.cancel(
                    f'Actor: {actor.uid} was task-context-cancelled with,\n'
                    f'str(internal_err)'
                )
            case _:
                log.exception(
                    'Main actor-runtime task errored\n'
                    f'<x)\n'
                    f' |_{actor}\n'
                )

        raise internal_err

    finally:
        teardown_report: str = (
            'Main actor-runtime task completed\n'
        )

        # ?TODO? should this be in `._entry`/`._root` mods instead?
        #
        # teardown any actor-lifetime-bound contexts
        ls: ExitStack = actor.lifetime_stack
        # only report if there are any registered
        cbs: list[Callable] = [
            repr(tup[1].__wrapped__)
            for tup in ls._exit_callbacks
        ]
        if cbs:
            cbs_str: str = '\n'.join(cbs)
            teardown_report += (
                '-> Closing actor-lifetime-bound callbacks\n\n'
                f'}}>\n'
                f' |_{ls}\n'
                f'   |_{cbs_str}\n'
            )
            # XXX NOTE XXX this will cause an error which
            # prevents any `infected_aio` actor from continuing
            # and any callbacks in the `ls` here WILL NOT be
            # called!!
            # await debug.pause(shield=True)

        ls.close()

        # XXX TODO but hard XXX
        # we can't actually do this bc the debugger uses the
        # _service_n to spawn the lock task, BUT, in theory if we had
        # the root nursery surround this finally block it might be
        # actually possible to debug THIS machinery in the same way
        # as user task code?
        #
        # if actor.name == 'brokerd.ib':
        #     with CancelScope(shield=True):
        #         await debug.breakpoint()

        # Unregister actor from the registry-sys / registrar.
        if (
            is_registered
            and not actor.is_registrar
        ):
            failed: bool = False
            for addr in actor.reg_addrs:
                waddr = wrap_address(addr)
                assert waddr.is_valid
                with trio.move_on_after(0.5) as cs:
                    cs.shield = True
                    try:
                        async with get_registry(
                            addr,
                        ) as reg_portal:
                            await reg_portal.run_from_ns(
                                'self',
                                'unregister_actor',
                                uid=actor.uid
                            )
                    except OSError:
                        failed = True
                if cs.cancelled_caught:
                    failed = True

                if failed:
                    teardown_report += (
                        f'-> Failed to unregister {actor.name} from '
                        f'registar @ {addr}\n'
                    )

        # Ensure all peers (actors connected to us as clients) are finished
        if (
            (ipc_server := actor.ipc_server)
            and
            ipc_server.has_peers(check_chans=True)
        ):
            teardown_report += (
                f'-> Waiting for remaining peers {ipc_server._peers} to clear..\n'
            )
            log.runtime(teardown_report)
            await ipc_server.wait_for_no_more_peers(
                shield=True,
            )

        teardown_report += (
            '-> All peer channels are complete\n'
        )

    teardown_report += (
        'Actor runtime exiting\n'
        f'>)\n'
        f'|_{actor}\n'
    )
    log.info(teardown_report)


# TODO: rename to `Registry` and move to `.discovery._registry`!
class Arbiter(Actor):
    '''
    A special registrar (and for now..) `Actor` who can contact all
    other actors within its immediate process tree and possibly keeps
    a registry of others meant to be discoverable in a distributed
    application. Normally the registrar is also the "root actor" and
    thus always has access to the top-most-level actor (process)
    nursery.

    By default, the registrar is always initialized when and if no
    other registrar socket addrs have been specified to runtime
    init entry-points (such as `open_root_actor()` or
    `open_nursery()`). Any time a new main process is launched (and
    thus thus a new root actor created) and, no existing registrar
    can be contacted at the provided `registry_addr`, then a new
    one is always created; however, if one can be reached it is
    used.

    Normally a distributed app requires at least registrar per
    logical host where for that given "host space" (aka localhost
    IPC domain of addresses) it is responsible for making all other
    host (local address) bound actors *discoverable* to external
    actor trees running on remote hosts.

    '''
    is_arbiter = True

    # TODO, implement this as a read on there existing a `._state` of
    # some sort setup by whenever we impl this all as
    # a `.discovery._registry.open_registry()` API
    def is_registry(self) -> bool:
        return self.is_arbiter

    def __init__(
        self,
        *args,
        **kwargs,
    ) -> None:

        self._registry: dict[
            tuple[str, str],
            UnwrappedAddress,
        ] = {}
        self._waiters: dict[
            str,
            # either an event to sync to receiving an actor uid (which
            # is filled in once the actor has sucessfully registered),
            # or that uid after registry is complete.
            list[trio.Event | tuple[str, str]]
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

        This method is async to allow for cross-actor invocation.

        '''
        # NOTE: requires ``strict_map_key=False`` to the msgpack
        # unpacker since we have tuples as keys (not this makes the
        # arbiter suscetible to hashdos):
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

        This is a blocking call if no actor by the provided name is currently
        registered.

        '''
        addrs: list[UnwrappedAddress] = []
        addr: UnwrappedAddress

        mailbox_info: str = 'Actor registry contact infos:\n'
        for uid, addr in self._registry.items():
            mailbox_info += (
                f'|_uid: {uid}\n'
                f'|_addr: {addr}\n\n'
            )
            if name == uid[0]:
                addrs.append(addr)

        if not addrs:
            waiter = trio.Event()
            self._waiters.setdefault(name, []).append(waiter)
            await waiter.wait()

            for uid in self._waiters[name]:
                if not isinstance(uid, trio.Event):
                    addrs.append(self._registry[uid])

        log.runtime(mailbox_info)
        return addrs

    async def register_actor(
        self,
        uid: tuple[str, str],
        addr: UnwrappedAddress
    ) -> None:
        uid = name, hash = (str(uid[0]), str(uid[1]))
        waddr: Address = wrap_address(addr)
        if not waddr.is_valid:
            # should never be 0-dynamic-os-alloc
            await debug.pause()

        self._registry[uid] = addr

        # pop and signal all waiter events
        events = self._waiters.pop(name, [])
        self._waiters.setdefault(name, []).append(uid)
        for event in events:
            if isinstance(event, trio.Event):
                event.set()

    async def unregister_actor(
        self,
        uid: tuple[str, str]

    ) -> None:
        uid = (str(uid[0]), str(uid[1]))
        entry: tuple = self._registry.pop(uid, None)
        if entry is None:
            log.warning(f'Request to de-register {uid} failed?')
