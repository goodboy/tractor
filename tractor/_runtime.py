tb = None

    cancel_scope = CancelScope()
    # activated cancel scope ref
    cs: CancelScope|None = None

    ctx = actor.get_context(
        chan=chan,
        cid=cid,
        nsf=NamespacePath.from_ref(func),

        # TODO: if we wanted to get cray and support it?
        # side='callee',

        # We shouldn't ever need to pass this through right?
        # it's up to the soon-to-be called rpc task to
        # open the stream with this option.
        # allow_overruns=True,
    )
    context: bool = False

    # TODO: deprecate this style..
    if getattr(func, '_tractor_stream_function', False):
        # handle decorated ``@tractor.stream`` async functions
        sig = inspect.signature(func)
        params = sig.parameters

        # compat with old api
        kwargs['ctx'] = ctx
        treat_as_gen = True

        if 'ctx' in params:
            warnings.warn(
                "`@tractor.stream decorated funcs should now declare "
                "a `stream`  arg, `ctx` is now designated for use with "
                "@tractor.context",
                DeprecationWarning,
                stacklevel=2,
            )

        elif 'stream' in params:
            assert 'stream' in params
            kwargs['stream'] = ctx


    elif getattr(func, '_tractor_context_function', False):
        # handle decorated ``@tractor.context`` async function
        kwargs['ctx'] = ctx
        context = True

    # errors raised inside this block are propgated back to caller
    async with _errors_relayed_via_ipc(
        actor,
        chan,
        ctx,
        is_rpc,
        hide_tb=hide_tb,
        task_status=task_status,
    ):
        if not (
            inspect.isasyncgenfunction(func) or
            inspect.iscoroutinefunction(func)
        ):
            raise TypeError(f'{func} must be an async function!')

        # init coroutine with `kwargs` to immediately catch any
        # type-sig errors.
        try:
            coro = func(**kwargs)
        except TypeError:
            raise

        # TODO: implement all these cases in terms of the
        # `Context` one!
        if not context:
            await _invoke_non_context(
                actor,
                cancel_scope,
                ctx,
                cid,
                chan,
                func,
                coro,
                kwargs,
                treat_as_gen,
                is_rpc,
                task_status,
            )
            # below is only for `@context` funcs
            return

        # our most general case: a remote SC-transitive,
        # IPC-linked, cross-actor-task "context"
        # ------ - ------
        # TODO: every other "func type" should be implemented from
        # a special case of this impl eventually!
        # -[ ] streaming funcs should instead of being async-for
        #     handled directly here wrapped in
        #     a async-with-open_stream() closure that does the
        #     normal thing you'd expect a far end streaming context
        #     to (if written by the app-dev).
        # -[ ] one off async funcs can literally just be called
        #     here and awaited directly, possibly just with a small
        #     wrapper that calls `Context.started()` and then does
        #     the `await coro()`?

        # a "context" endpoint type is the most general and
        # "least sugary" type of RPC ep with support for
        # bi-dir streaming B)
        await chan.send({
            'functype': 'context',
            'cid': cid
        })

        # TODO: should we also use an `.open_context()` equiv
        # for this callee side by factoring the impl from
        # `Portal.open_context()` into a common helper?
        #
        # NOTE: there are many different ctx state details
        # in a callee side instance according to current impl:
        # - `.cancelled_caught` can never be `True`.
        #  -> the below scope is never exposed to the
        #     `@context` marked RPC function.
        # - `._portal` is never set.
        try:
            async with trio.open_nursery() as tn:
                ctx._scope_nursery = tn
                ctx._scope = tn.cancel_scope
                task_status.started(ctx)

                # TODO: should would be nice to have our
                # `TaskMngr` nursery here!
                res: Any = await coro
                ctx._result = res

                # deliver final result to caller side.
                await chan.send({
                    'return': res,
                    'cid': cid
                })

            # NOTE: this happens IFF `ctx._scope.cancel()` is
            # called by any of,
            # - *this* callee task manually calling `ctx.cancel()`.
            # - the runtime calling `ctx._deliver_msg()` which
            #   itself calls `ctx._maybe_cancel_and_set_remote_error()`
            #   which cancels the scope presuming the input error
            #   is not a `.cancel_acked` pleaser.
            # - currently a never-should-happen-fallthrough case
            #   inside ._context._drain_to_final_msg()`..
            #   # TODO: remove this ^ right?
            if ctx._scope.cancelled_caught:
                our_uid: tuple = actor.uid

                # first check for and raise any remote error
                # before raising any context cancelled case
                # so that real remote errors don't get masked as
                # ``ContextCancelled``s.
                if re := ctx._remote_error:
                    ctx._maybe_raise_remote_err(re)

                cs: CancelScope = ctx._scope

                if cs.cancel_called:

                    canceller: tuple = ctx.canceller
                    msg: str = (
                        'actor was cancelled by '
                    )

                    # NOTE / TODO: if we end up having
                    # ``Actor._cancel_task()`` call
                    # ``Context.cancel()`` directly, we're going to
                    # need to change this logic branch since it
                    # will always enter..
                    if ctx._cancel_called:
                        # TODO: test for this!!!!!
                        canceller: tuple = our_uid
                        msg += 'itself '

                    # if the channel which spawned the ctx is the
                    # one that cancelled it then we report that, vs.
                    # it being some other random actor that for ex.
                    # some actor who calls `Portal.cancel_actor()`
                    # and by side-effect cancels this ctx.
                    elif canceller == ctx.chan.uid:
                        msg += 'its caller'

                    else:
                        msg += 'a remote peer'

                    div_chars: str = '------ - ------'
                    div_offset: int = (
                        round(len(msg)/2)+1
                        +
                        round(len(div_chars)/2)+1
                    )
                    div_str: str = (
                        '\n'
                        +
                        ' '*div_offset
                        +
                        f'{div_chars}\n'
                    )
                    msg += (
                        div_str +
                        f'<= canceller: {canceller}\n'
                        f'=> uid: {our_uid}\n'
                        f'  |_{ctx._task}()'

                        # TODO: instead just show the
                        # ctx.__str__() here?
                        # -[ ] textwrap.indent() it correctly!
                        # -[ ] BUT we need to wait until
                        #   the state is filled out before emitting
                        #   this msg right ow its kinda empty? bleh..
                        #
                        # f'  |_{ctx}'
                    )

                    # task-contex was either cancelled by request using
                    # ``Portal.cancel_actor()`` or ``Context.cancel()``
                    # on the far end, or it was cancelled by the local
                    # (callee) task, so relay this cancel signal to the
                    # other side.
                    ctxc = ContextCancelled(
                        msg,
                        suberror_type=trio.Cancelled,
                        canceller=canceller,
                    )
                    # assign local error so that the `.outcome`
                    # resolves to an error for both reporting and
                    # state checks.
                    ctx._local_error = ctxc
                    raise ctxc

        # XXX: do we ever trigger this block any more?
        except (
            BaseExceptionGroup,
            trio.Cancelled,
            BaseException,

        ) as scope_error:

            # always set this (callee) side's exception as the
            # local error on the context
            ctx._local_error: BaseException = scope_error

            # if a remote error was set then likely the
            # exception group was raised due to that, so
            # and we instead raise that error immediately!
            ctx.maybe_raise()

            # maybe TODO: pack in come kinda
            # `trio.Cancelled.__traceback__` here so they can be
            # unwrapped and displayed on the caller side? no se..
            raise

        # `@context` entrypoint task bookeeping.
        # i.e. only pop the context tracking if used ;)
        finally:
            assert chan.uid

            # don't pop the local context until we know the
            # associated child isn't in debug any more
            await _debug.maybe_wait_for_debugger()
            ctx: Context = actor._contexts.pop((
                chan.uid,
                cid,
                # ctx.side,
            ))

            merr: Exception|None = ctx.maybe_error

            (
                res_type_str,
                res_str,
            ) = (
                ('error', f'{type(merr)}',)
                if merr
                else (
                    'result',
                    f'`{repr(ctx.outcome)}`',
                )
            )
            log.cancel(
                f'IPC context terminated with a final {res_type_str}\n\n'
                f'{ctx}\n'
            )


def _get_mod_abspath(module):
    return os.path.abspath(module.__file__)


async def try_ship_error_to_remote(
    channel: Channel,
    err: Exception|BaseExceptionGroup,

    cid: str|None = None,
    remote_descr: str = 'parent',
    hide_tb: bool = True,

) -> None:
    '''
    Box, pack and encode a local runtime(-internal) exception for
    an IPC channel `.send()` with transport/network failures and
    local cancellation ignored but logged as critical(ly bad).

    '''
    __tracebackhide__: bool = hide_tb
    with CancelScope(shield=True):
        try:
            # NOTE: normally only used for internal runtime errors
            # so ship to peer actor without a cid.
            msg: dict = pack_error(
                err,
                cid=cid,

                # TODO: special tb fmting for ctxc cases?
                # tb=tb,
            )
            # NOTE: the src actor should always be packed into the
            # error.. but how should we verify this?
            # actor: Actor = _state.current_actor()
            # assert err_msg['src_actor_uid']
            # if not err_msg['error'].get('src_actor_uid'):
            #     import pdbp; pdbp.set_trace()
            await channel.send(msg)

        # XXX NOTE XXX in SC terms this is one of the worst things
        # that can happen and provides for a 2-general's dilemma..
        except (
            trio.ClosedResourceError,
            trio.BrokenResourceError,
            BrokenPipeError,
        ):
            err_msg: dict = msg['error']['tb_str']
            log.critical(
                'IPC transport failure -> '
                f'failed to ship error to {remote_descr}!\n\n'
                f'X=> {channel.uid}\n\n'
                f'{err_msg}\n'
            )


class Actor:
    '''
    The fundamental "runtime" concurrency primitive.

    An *actor* is the combination of a regular Python process executing
    a ``trio`` task tree, communicating with other actors through
    "memory boundary portals" - which provide a native async API around
    IPC transport "channels" which themselves encapsulate various
    (swappable) network protocols.


    Each "actor" is ``trio.run()`` scheduled "runtime" composed of
    many concurrent tasks in a single thread. The "runtime" tasks
    conduct a slew of low(er) level functions to make it possible
    for message passing between actors as well as the ability to
    create new actors (aka new "runtimes" in new processes which
    are supervised via a nursery construct). Each task which sends
    messages to a task in a "peer" (not necessarily a parent-child,
    depth hierarchy) is able to do so via an "address", which maps
    IPC connections across memory boundaries, and a task request id
    which allows for per-actor tasks to send and receive messages
    to specific peer-actor tasks with which there is an ongoing
    RPC/IPC dialog.

    '''
    # ugh, we need to get rid of this and replace with a "registry" sys
    # https://github.com/goodboy/tractor/issues/216
    is_arbiter: bool = False
    msg_buffer_size: int = 2**6

    # nursery placeholders filled in by `async_main()` after fork
    _root_n: Nursery | None = None
    _service_n: Nursery | None = None
    _server_n: Nursery | None = None

    # Information about `__main__` from parent
    _parent_main_data: dict[str, str]
    _parent_chan_cs: CancelScope | None = None

    # syncs for setup/teardown sequences
    _server_down: trio.Event | None = None

    # user toggled crash handling (including monkey-patched in
    # `trio.open_nursery()` via `.trionics._supervisor` B)
    _debug_mode: bool = False

    # if started on ``asycio`` running ``trio`` in guest mode
    _infected_aio: bool = False

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
        *,
        enable_modules: list[str] = [],
        uid: str | None = None,
        loglevel: str | None = None,
        arbiter_addr: tuple[str, int] | None = None,
        spawn_method: str | None = None
    ) -> None:
        '''
        This constructor is called in the parent actor **before** the spawning
        phase (aka before a new process is executed).

        '''
        self.name = name
        self.uid = (
            name,
            uid or str(uuid.uuid4())
        )

        self._cancel_complete = trio.Event()
        self._cancel_called_by_remote: tuple[str, tuple] | None = None
        self._cancel_called: bool = False

        # retreive and store parent `__main__` data which
        # will be passed to children
        self._parent_main_data = _mp_fixup_main._mp_figure_out_main()

        # always include debugging tools module
        enable_modules.append('tractor._debug')

        mods = {}
        for name in enable_modules:
            mod = importlib.import_module(name)
            mods[name] = _get_mod_abspath(mod)

        self.enable_modules = mods
        self._mods: dict[str, ModuleType] = {}
        self.loglevel = loglevel

        self._arb_addr: tuple[str, int] | None = (
            str(arbiter_addr[0]),
            int(arbiter_addr[1])
        ) if arbiter_addr else None

        # marked by the process spawning backend at startup
        # will be None for the parent most process started manually
        # by the user (currently called the "arbiter")
        self._spawn_method = spawn_method

        self._peers: defaultdict = defaultdict(list)
        self._peer_connected: dict = {}
        self._no_more_peers = trio.Event()
        self._no_more_peers.set()
        self._ongoing_rpc_tasks = trio.Event()
        self._ongoing_rpc_tasks.set()

        # (chan, cid) -> (cancel_scope, func)
        self._rpc_tasks: dict[
            tuple[Channel, str],
            tuple[Context, Callable, trio.Event]
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

        self._listeners: list[trio.abc.Listener] = []
        self._parent_chan: Channel | None = None
        self._forkserver_info: tuple | None = None
        self._actoruid2nursery: dict[
            tuple[str, str],
            ActorNursery | None,
        ] = {}  # type: ignore  # noqa

    async def wait_for_peer(
        self, uid: tuple[str, str]
    ) -> tuple[trio.Event, Channel]:
        '''
        Wait for a connection back from a spawned actor with a given
        ``uid``.

        '''
        log.runtime(f"Waiting for peer {uid} to connect")
        event = self._peer_connected.setdefault(uid, trio.Event())
        await event.wait()
        log.runtime(f"{uid} successfully connected back to us")
        return event, self._peers[uid][-1]

    def load_modules(
        self,
        debug_mode: bool = False,
    ) -> None:
        '''
        Load allowed RPC modules locally (after fork).

        Since this actor may be spawned on a different machine from
        the original nursery we need to try and load the local module
        code (if it exists).

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

            for modpath, filepath in self.enable_modules.items():
                # XXX append the allowed module to the python path which
                # should allow for relative (at least downward) imports.
                sys.path.append(os.path.dirname(filepath))
                log.runtime(f"Attempting to import {modpath}@{filepath}")
                mod = importlib.import_module(modpath)
                self._mods[modpath] = mod
                if modpath == '__main__':
                    self._mods['__mp_main__'] = mod

        except ModuleNotFoundError:
            # it is expected the corresponding `ModuleNotExposed` error
            # will be raised later
            log.error(
                f"Failed to import {modpath} in {self.name}"
            )
            raise

    def _get_rpc_func(self, ns, funcname):
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

    async def _stream_handler(

        self,
        stream: trio.SocketStream,

    ) -> None:
        '''
        Entry point for new inbound connections to the channel server.

        '''
        self._no_more_peers = trio.Event()  # unset by making new
        chan = Channel.from_stream(stream)
        their_uid: tuple[str, str]|None = chan.uid

        con_msg: str = ''
        if their_uid:
            # NOTE: `.uid` is only set after first contact
            con_msg = (
                'IPC Re-connection from already known peer? '
            )
        else:
            con_msg = (
                'New IPC connection to us '
            )

        con_msg += (
            f'<= @{chan.raddr}\n'
            f'|_{chan}\n'
            # f' |_@{chan.raddr}\n\n'
        )
        # send/receive initial handshake response
        try:
            uid: tuple|None = await self._do_handshake(chan)
        except (
            # we need this for ``msgspec`` for some reason?
            # for now, it's been put in the stream backend.
            # trio.BrokenResourceError,
            # trio.ClosedResourceError,

            TransportClosed,
        ):
            # XXX: This may propagate up from ``Channel._aiter_recv()``
            # and ``MsgpackStream._inter_packets()`` on a read from the
            # stream particularly when the runtime is first starting up
            # inside ``open_root_actor()`` where there is a check for
            # a bound listener on the "arbiter" addr.  the reset will be
            # because the handshake was never meant took place.
            log.warning(
                con_msg
                +
                ' -> But failed to handshake? Ignoring..\n'
            )
            return

        con_msg += (
            f' -> Handshake with actor `{uid[0]}[{uid[1][-6:]}]` complete\n'
        )
        # IPC connection tracking for both peers and new children:
        # - if this is a new channel to a locally spawned
        #   sub-actor there will be a spawn wait even registered
        #   by a call to `.wait_for_peer()`.
        # - if a peer is connecting no such event will exit.
        event: trio.Event|None = self._peer_connected.pop(
            uid,
            None,
        )
        if event:
            con_msg += (
                ' -> Waking subactor spawn waiters: '
                f'{event.statistics().tasks_waiting}\n'
                f' -> Registered IPC chan for child actor {uid}@{chan.raddr}\n'
                # f'    {event}\n'
                # f'    |{event.statistics()}\n'
            )
            # wake tasks waiting on this IPC-transport "connect-back"
            event.set()

        else:
            con_msg += (
                f' -> Registered IPC chan for peer actor {uid}@{chan.raddr}\n'
            )  # type: ignore

        chans: list[Channel] = self._peers[uid]
        # if chans:
        #     # TODO: re-use channels for new connections instead
        #     # of always new ones?
        #     # => will require changing all the discovery funcs..

        # append new channel
        # TODO: can we just use list-ref directly?
        chans.append(chan)

        log.runtime(con_msg)

        # Begin channel management - respond to remote requests and
        # process received reponses.
        disconnected: bool = False
        try:
            disconnected: bool = await process_messages(
                self,
                chan,
            )
        except trio.Cancelled:
            log.cancel(
                'IPC transport msg loop was cancelled for \n'
                f'|_{chan}\n'
            )
            raise

        finally:
            local_nursery: (
                ActorNursery|None
            ) = self._actoruid2nursery.get(uid)

            # This is set in ``Portal.cancel_actor()``. So if
            # the peer was cancelled we try to wait for them
            # to tear down their side of the connection before
            # moving on with closing our own side.
            if local_nursery:
                if chan._cancel_called:
                    log.cancel(
                        'Waiting on cancel request to peer\n'
                        f'`Portal.cancel_actor()` => {chan.uid}\n'
                    )

                # XXX: this is a soft wait on the channel (and its
                # underlying transport protocol) to close from the
                # remote peer side since we presume that any channel
                # which is mapped to a sub-actor (i.e. it's managed by
                # one of our local nurseries) has a message is sent to
                # the peer likely by this actor (which is now in
                # a cancelled condition) when the local runtime here is
                # now cancelled while (presumably) in the middle of msg
                # loop processing.
                with trio.move_on_after(0.5) as cs:
                    cs.shield = True

                    # attempt to wait for the far end to close the
                    # channel and bail after timeout (a 2-generals
                    # problem on closure).
                    assert chan.transport
                    async for msg in chan.transport.drain():

                        # try to deliver any lingering msgs
                        # before we destroy the channel.
                        # This accomplishes deterministic
                        # ``Portal.cancel_actor()`` cancellation by
                        # making sure any RPC response to that call is
                        # delivered the local calling task.
                        # TODO: factor this into a helper?
                        log.warning(
                            'Draining msg from disconnected peer\n'
                            f'{chan.uid}\n'
                            f'|_{chan}\n'
                            f'  |_{chan.transport}\n\n'

                            f'{pformat(msg)}\n'
                        )
                        cid = msg.get('cid')
                        if cid:
                            # deliver response to local caller/waiter
                            await self._push_result(
                                chan,
                                cid,
                                msg,
                            )

                    # NOTE: when no call to `open_root_actor()` was
                    # made, we implicitly make that call inside
                    # the first `.open_nursery()`, in this case we
                    # can assume that we are the root actor and do
                    # not have to wait for the nursery-enterer to
                    # exit before shutting down the actor runtime.
                    #
                    # see matching  note inside `._supervise.open_nursery()`
                    if not local_nursery._implicit_runtime_started:
                        log.runtime(
                            'Waiting on local actor nursery to exit..\n'
                            f'|_{local_nursery}\n'
                        )
                        await local_nursery.exited.wait()

                if (
                    cs.cancelled_caught
                    and not local_nursery._implicit_runtime_started
                ):
                    log.warning(
                        'Failed to exit local actor nursery?\n'
                        f'|_{local_nursery}\n'
                    )
                    # await _debug.pause()

                if disconnected:
                    # if the transport died and this actor is still
                    # registered within a local nursery, we report that the
                    # IPC layer may have failed unexpectedly since it may be
                    # the cause of other downstream errors.
                    entry = local_nursery._children.get(uid)
                    if entry:
                        proc: trio.Process
                        _, proc, _ = entry

                        poll = getattr(proc, 'poll', None)
                        if poll and poll() is None:
                            log.cancel(
                                f'Peer IPC broke but subproc is alive?\n\n'

                                f'<=x {chan.uid}@{chan.raddr}\n'
                                f'   |_{proc}\n'
                            )

            # ``Channel`` teardown and closure sequence
            # drop ref to channel so it can be gc-ed and disconnected
            log.runtime(
                f'Disconnected IPC channel:\n'
                f'uid: {chan.uid}\n'
                f'|_{pformat(chan)}\n'
            )
            chans.remove(chan)

            # TODO: do we need to be this pedantic?
            if not chans:
                log.runtime(
                    f'No more channels with {chan.uid}'
                )
                self._peers.pop(uid, None)

            peers_str: str = ''
            for uid, chans in self._peers.items():
                peers_str += (
                    f'|_ uid: {uid}\n'
                )
                for i, chan in enumerate(chans):
                    peers_str += (
                        f' |_[{i}] {pformat(chan)}\n'
                    )

            log.runtime(
                f'Remaining IPC {len(self._peers)} peers:\n'
                + peers_str
            )

            # No more channels to other actors (at all) registered
            # as connected.
            if not self._peers:
                log.runtime("Signalling no more peer channel connections")
                self._no_more_peers.set()

                # NOTE: block this actor from acquiring the
                # debugger-TTY-lock since we have no way to know if we
                # cancelled it and further there is no way to ensure the
                # lock will be released if acquired due to having no
                # more active IPC channels.
                if _state.is_root_process():
                    pdb_lock = _debug.Lock
                    pdb_lock._blocked.add(uid)

                    # TODO: NEEEDS TO BE TESTED!
                    # actually, no idea if this ever even enters.. XD
                    pdb_user_uid: tuple = pdb_lock.global_actor_in_debug
                    if (
                        pdb_user_uid
                        and local_nursery
                    ):
                        entry: tuple|None = local_nursery._children.get(pdb_user_uid)
                        if entry:
                            proc: trio.Process
                            _, proc, _ = entry

                        if (
                            (poll := getattr(proc, 'poll', None))
                            and poll() is None
                        ):
                            log.cancel(
                                'Root actor reports no-more-peers, BUT '
                                'a DISCONNECTED child still has the debug '
                                'lock!\n'
                                f'root uid: {self.uid}\n'
                                f'last disconnected child uid: {uid}\n'
                                f'locking child uid: {pdb_user_uid}\n'
                            )
                            await _debug.maybe_wait_for_debugger(
                                child_in_debug=True
                            )

                    # TODO: just bc a child's transport dropped
                    # doesn't mean it's not still using the pdb
                    # REPL! so,
                    # -[ ] ideally we can check out child proc
                    #  tree to ensure that its alive (and
                    #  actually using the REPL) before we cancel
                    #  it's lock acquire by doing the below!
                    # -[ ] create a way to read the tree of each actor's
                    #  grandchildren such that when an
                    #  intermediary parent is cancelled but their
                    #  child has locked the tty, the grandparent
                    #  will not allow the parent to cancel or
                    #  zombie reap the child! see open issue:
                    #  - https://github.com/goodboy/tractor/issues/320
                    # ------ - ------
                    # if a now stale local task has the TTY lock still
                    # we cancel it to allow servicing other requests for
                    # the lock.
                    db_cs: trio.CancelScope|None = pdb_lock._root_local_task_cs_in_debug
                    if (
                        db_cs
                        and not db_cs.cancel_called
                        and uid == pdb_user_uid
                    ):
                        log.warning(
                            f'STALE DEBUG LOCK DETECTED FOR {uid}'
                        )
                        # TODO: figure out why this breaks tests..
                        db_cs.cancel()

            # XXX: is this necessary (GC should do it)?
            # XXX WARNING XXX
            # Be AWARE OF THE INDENT LEVEL HERE
            # -> ONLY ENTER THIS BLOCK WHEN ._peers IS
            # EMPTY!!!!
            if (
                not self._peers
                and chan.connected()
            ):
                    # if the channel is still connected it may mean the far
                    # end has not closed and we may have gotten here due to
                    # an error and so we should at least try to terminate
                    # the channel from this end gracefully.
                    log.runtime(
                        'Terminating channel with `None` setinel msg\n'
                        f'|_{chan}\n'
                    )
                    try:
                        # send a msg loop terminate sentinel
                        await chan.send(None)

                        # XXX: do we want this?
                        # causes "[104] connection reset by peer" on other end
                        # await chan.aclose()

                    except trio.BrokenResourceError:
                        log.runtime(f"Channel {chan.uid} was already closed")

    async def _push_result(
        self,
        chan: Channel,
        cid: str,
        msg: dict[str, Any],

    ) -> None|bool:
        '''
        Push an RPC result to the local consumer's queue.

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
            log.warning(
                'Ignoring invalid IPC ctx msg!\n\n'
                f'<= sender: {uid}\n'
                f'=> cid: {cid}\n\n'

                f'{msg}\n'
            )
            return

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

        msg_buffer_size: int | None = None,
        allow_overruns: bool = False,

    ) -> Context:
        '''
        Look up or create a new inter-actor-task-IPC-linked task
        "context" which encapsulates the local task's scheduling
        enviroment including a ``trio`` cancel scope, a pair of IPC
        messaging "feeder" channels, and an RPC id unique to the
        task-as-function invocation.

        '''
        actor_uid = chan.uid
        assert actor_uid
        try:
            ctx = self._contexts[(
                actor_uid,
                cid,
                # side,
            )]
            log.runtime(
                f'Retreived cached IPC ctx for\n'
                f'peer: {chan.uid}\n'
                f'cid:{cid}\n'
            )
            ctx._allow_overruns = allow_overruns

            # adjust buffer size if specified
            state = ctx._send_chan._state  # type: ignore
            if msg_buffer_size and state.max_buffer_size != msg_buffer_size:
                state.max_buffer_size = msg_buffer_size

        except KeyError:
            log.runtime(
                f'Creating NEW IPC ctx for\n'
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

        # IPC channel config
        msg_buffer_size: int | None = None,
        allow_overruns: bool = False,
        load_nsf: bool = False,

    ) -> Context:
        '''
        Send a ``'cmd'`` message to a remote actor, which starts
        a remote task-as-function entrypoint.

        Synchronously validates the endpoint type  and return a caller
        side task ``Context`` that can be used to wait for responses
        delivered by the local runtime's message processing loop.

        '''
        cid = str(uuid.uuid4())
        assert chan.uid
        ctx = self.get_context(
            chan=chan,
            cid=cid,
            nsf=nsf,

            # side='caller',
            msg_buffer_size=msg_buffer_size,
            allow_overruns=allow_overruns,
        )

        if (
            'self' in nsf
            or not load_nsf
        ):
            ns, _, func = nsf.partition(':')
        else:
            # TODO: pass nsf directly over wire!
            # -[ ] but, how to do `self:<Actor.meth>`??
            ns, func = nsf.to_tuple()

        log.runtime(
            'Sending cmd to\n'
            f'peer: {chan.uid} => \n'
            '\n'
            f'=> {ns}.{func}({kwargs})\n'
        )
        await chan.send(
            {'cmd': (
                ns,
                func,
                kwargs,
                self.uid,
                cid,
            )}
        )

        # Wait on first response msg and validate; this should be
        # immediate.
        first_msg: dict = await ctx._recv_chan.receive()
        functype: str = first_msg.get('functype')

        if 'error' in first_msg:
            raise unpack_error(first_msg, chan)

        elif functype not in (
            'asyncfunc',
            'asyncgen',
            'context',
        ):
            raise ValueError(f"{first_msg} is an invalid response packet?")

        ctx._remote_func_type = functype
        return ctx

    async def _from_parent(
        self,
        parent_addr: tuple[str, int] | None,
    ) -> tuple[Channel, tuple[str, int] | None]:
        try:
            # Connect back to the parent actor and conduct initial
            # handshake. From this point on if we error, we
            # attempt to ship the exception back to the parent.
            chan = Channel(
                destaddr=parent_addr,
            )
            await chan.connect()

            # Initial handshake: swap names.
            await self._do_handshake(chan)

            accept_addr: tuple[str, int] | None = None

            if self._spawn_method == "trio":
                # Receive runtime state from our parent
                parent_data: dict[str, Any]
                parent_data = await chan.recv()
                log.runtime(
                    'Received state from parent:\n\n'
                    # TODO: eventually all these msgs as
                    # `msgspec.Struct` with a special mode that
                    # pformats them in multi-line mode, BUT only
                    # if "trace"/"util" mode is enabled?
                    f'{pformat(parent_data)}\n'
                )
                accept_addr = (
                    parent_data.pop('bind_host'),
                    parent_data.pop('bind_port'),
                )
                rvs = parent_data.pop('_runtime_vars')

                if rvs['_debug_mode']:
                    try:
                        log.info('Enabling `stackscope` traces on SIGUSR1')
                        from .devx import enable_stack_on_sig
                        enable_stack_on_sig()
                    except ImportError:
                        log.warning(
                            '`stackscope` not installed for use in debug mode!'
                        )

                log.runtime(f"Runtime vars are: {rvs}")
                rvs['_is_root'] = False
                _state._runtime_vars.update(rvs)

                for attr, value in parent_data.items():

                    if attr == '_arb_addr':
                        # XXX: ``msgspec`` doesn't support serializing tuples
                        # so just cash manually here since it's what our
                        # internals expect.
                        value = tuple(value) if value else None
                        self._arb_addr = value

                    else:
                        setattr(self, attr, value)

            return chan, accept_addr

        except OSError:  # failed to connect
            log.warning(
                f'Failed to connect to parent!?\n\n'
                'Closing IPC [TCP] transport server to\n'
                f'{parent_addr}\n'
                f'|_{self}\n\n'
            )
            await self.cancel(chan=None)  # self cancel
            raise

    async def _serve_forever(
        self,
        handler_nursery: Nursery,
        *,
        # (host, port) to bind for channel server
        accept_host: tuple[str, int] | None = None,
        accept_port: int = 0,
        task_status: TaskStatus[trio.Nursery] = trio.TASK_STATUS_IGNORED,
    ) -> None:
        '''
        Start the channel server, begin listening for new connections.

        This will cause an actor to continue living (blocking) until
        ``cancel_server()`` is called.

        '''
        self._server_down = trio.Event()
        try:
            async with trio.open_nursery() as server_n:
                listeners: list[trio.abc.Listener] = await server_n.start(
                    partial(
                        trio.serve_tcp,
                        self._stream_handler,
                        # new connections will stay alive even if this server
                        # is cancelled
                        handler_nursery=handler_nursery,
                        port=accept_port,
                        host=accept_host,
                    )
                )
                sockets: list[trio.socket] = [
                    getattr(listener, 'socket', 'unknown socket')
                    for listener in listeners
                ]
                log.runtime(
                    'Started TCP server(s)\n'
                    f'|_{sockets}\n'
                )
                self._listeners.extend(listeners)
                task_status.started(server_n)
        finally:
            # signal the server is down since nursery above terminated
            self._server_down.set()

    def cancel_soon(self) -> None:
        '''
        Cancel this actor asap; can be called from a sync context.

        Schedules `.cancel()` to be run immediately just like when
        cancelled by the parent.

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
        # implementation does start all such tasks in
        # a sub-nursery.
        req_chan: Channel|None,

    ) -> bool:
        '''
        Cancel this actor's runtime, eventually resulting in
        the exit its containing process.

        The ideal "deterministic" teardown sequence in order is:
         - cancel all ongoing rpc tasks by cancel scope
         - cancel the channel server to prevent new inbound
           connections
         - cancel the "service" nursery reponsible for
           spawning new rpc tasks
         - return control the parent channel message loop

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
            f'Runtime cancel request from {requester_type}:\n\n'
            f'<= .cancel(): {requesting_uid}\n'
        )

        # TODO: what happens here when we self-cancel tho?
        self._cancel_called_by_remote: tuple = requesting_uid
        self._cancel_called = True

        # cancel all ongoing rpc tasks
        with CancelScope(shield=True):

            # kill any debugger request task to avoid deadlock
            # with the root actor in this tree
            dbcs = _debug.Lock._debugger_request_cs
            if dbcs is not None:
                msg += (
                    '>> Cancelling active debugger request..\n'
                    f'|_{_debug.Lock}\n'
                )
                dbcs.cancel()

            # self-cancel **all** ongoing RPC tasks
            await self.cancel_rpc_tasks(
                req_uid=requesting_uid,
                parent_chan=None,
            )

            # stop channel server
            self.cancel_server()
            if self._server_down is not None:
                await self._server_down.wait()
            else:
                log.warning(
                    'Transport[TCP] server was cancelled start?'
                )

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
        Cancel a local task by call-id / channel.

        Note this method will be treated as a streaming function
        by remote actor-callers due to the declaration of ``ctx``
        in the signature (for now).

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
            # - callee returns a result before cancel-msg/ctxc-raised
            # - callee self raises ctxc before caller send request,
            # - callee errors prior to cancel req.
            log.cancel(
                'Cancel request invalid, RPC task already completed?\n'
                f'<= canceller: {requesting_uid}\n\n'
                f'=>{parent_chan}\n'
                f'  |_ctx-id: {cid}\n'
            )
            return True

        log.cancel(
            'Cancel request for RPC task\n\n'
            f'<= Actor._cancel_task(): {requesting_uid}\n\n'
            f'=> {ctx._task}\n'
            f'  |_ >> {ctx.repr_rpc}\n'
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
            f'  |_{ctx}\n'
        )
        log.runtime(
            'Waiting on RPC task to cancel\n'
            f'{flow_info}'
        )
        await is_complete.wait()
        log.runtime(
            f'Sucessfully cancelled RPC task\n'
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
        Cancel all existing RPC responder tasks using the cancel scope
        registered for each.

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
            f'|_{parent_chan}'
            if parent_chan
            else ''
        )
        log.cancel(
            f'Cancelling {descr} {len(tasks)} rpc tasks\n\n'
            f'<= `Actor.cancel_rpc_tasks()`: {req_uid}\n'
            f'    {rent_chan_repr}\n'
            # f'{self}\n'
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
                f'|_{tasks}'
            )
        await self._ongoing_rpc_tasks.wait()

    def cancel_server(self) -> None:
        '''
        Cancel the internal channel server nursery thereby
        preventing any new inbound connections from being established.

        '''
        if self._server_n:
            log.runtime("Shutting down channel server")
            self._server_n.cancel_scope.cancel()

    @property
    def accept_addr(self) -> tuple[str, int] | None:
        '''
        Primary address to which the channel server is bound.

        '''
        # throws OSError on failure
        return self._listeners[0].socket.getsockname()  # type: ignore

    def get_parent(self) -> Portal:
        '''
        Return a portal to our parent actor.

        '''
        assert self._parent_chan, "No parent channel for this actor?"
        return Portal(self._parent_chan)

    def get_chans(self, uid: tuple[str, str]) -> list[Channel]:
        '''
        Return all channels to the actor with provided uid.

        '''
        return self._peers[uid]

    async def _do_handshake(
        self,
        chan: Channel

    ) -> tuple[str, str]:
        '''
        Exchange `(name, UUIDs)` identifiers as the first
        communication step.

        These are essentially the "mailbox addresses" found in
        actor model parlance.

        '''
        await chan.send(self.uid)
        value: tuple = await chan.recv()
        uid: tuple[str, str] = (str(value[0]), str(value[1]))

        if not isinstance(uid, tuple):
            raise ValueError(f"{uid} is not a valid uid?!")

        chan.uid = str(uid[0]), str(uid[1])
        return uid

    def is_infected_aio(self) -> bool:
        return self._infected_aio


async def async_main(
    actor: Actor,
    accept_addr: tuple[str, int] | None = None,

    # XXX: currently ``parent_addr`` is only needed for the
    # ``multiprocessing`` backend (which pickles state sent to
    # the child instead of relaying it over the connect-back
    # channel). Once that backend is removed we can likely just
    # change this to a simple ``is_subactor: bool`` which will
    # be False when running as root actor and True when as
    # a subactor.
    parent_addr: tuple[str, int] | None = None,
    task_status: TaskStatus[None] = trio.TASK_STATUS_IGNORED,

) -> None:
    '''
    Actor runtime entrypoint; start the IPC channel server, maybe connect
    back to the parent, and startup all core machinery tasks.

    A "root" (or "top-level") nursery for this actor is opened here and
    when cancelled/terminated effectively closes the actor's "runtime".

    '''
    # attempt to retreive ``trio``'s sigint handler and stash it
    # on our debugger lock state.
    _debug.Lock._trio_handler = signal.getsignal(signal.SIGINT)

    registered_with_arbiter = False
    try:

        # establish primary connection with immediate parent
        actor._parent_chan = None
        if parent_addr is not None:

            actor._parent_chan, accept_addr_rent = await actor._from_parent(
                parent_addr)

            # either it's passed in because we're not a child
            # or because we're running in mp mode
            if accept_addr_rent is not None:
                accept_addr = accept_addr_rent


        # The "root" nursery ensures the channel with the immediate
        # parent is kept alive as a resilient service until
        # cancellation steps have (mostly) occurred in
        # a deterministic way.
        async with trio.open_nursery() as root_nursery:
            actor._root_n = root_nursery
            assert actor._root_n

            async with trio.open_nursery() as service_nursery:
                # This nursery is used to handle all inbound
                # connections to us such that if the TCP server
                # is killed, connections can continue to process
                # in the background until this nursery is cancelled.
                actor._service_n = service_nursery
                assert actor._service_n

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
                #     _debug.pause_from_sync()
                #     import pdbp; pdbp.set_trace()
                #     raise

                # Startup up the transport(-channel) server with,
                # - subactor: the bind address is sent by our parent
                #   over our established channel
                # - root actor: the ``accept_addr`` passed to this method
                assert accept_addr
                host, port = accept_addr

                actor._server_n = await service_nursery.start(
                    partial(
                        actor._serve_forever,
                        service_nursery,
                        accept_host=host,
                        accept_port=port
                    )
                )
                accept_addr = actor.accept_addr
                if _state._runtime_vars['_is_root']:
                    _state._runtime_vars['_root_mailbox'] = accept_addr

                # Register with the arbiter if we're told its addr
                log.runtime(f"Registering {actor} for role `{actor.name}`")
                assert isinstance(actor._arb_addr, tuple)

                async with get_arbiter(*actor._arb_addr) as arb_portal:
                    await arb_portal.run_from_ns(
                        'self',
                        'register_actor',
                        uid=actor.uid,
                        sockaddr=accept_addr,
                    )

                registered_with_arbiter = True

                # init steps complete
                task_status.started()

                # Begin handling our new connection back to our
                # parent. This is done last since we don't want to
                # start processing parent requests until our channel
                # server is 100% up and running.
                if actor._parent_chan:
                    await root_nursery.start(
                        partial(
                            process_messages,
                            actor,
                            actor._parent_chan,
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
    except Exception as err:
        log.runtime("Closing all actor lifetime contexts")
        actor.lifetime_stack.close()

        if not registered_with_arbiter:
            # TODO: I guess we could try to connect back
            # to the parent through a channel and engage a debugger
            # once we have that all working with std streams locking?
            log.exception(
                f"Actor errored and failed to register with arbiter "
                f"@ {actor._arb_addr}?")
            log.error(
                "\n\n\t^^^ THIS IS PROBABLY A TRACTOR BUGGGGG!!! ^^^\n"
                "\tCALMLY CALL THE AUTHORITIES AND HIDE YOUR CHILDREN.\n\n"
                "\tYOUR PARENT CODE IS GOING TO KEEP WORKING FINE!!!\n"
                "\tTHIS IS HOW RELIABlE SYSTEMS ARE SUPPOSED TO WORK!?!?\n"
            )

        if actor._parent_chan:
            await try_ship_error_to_remote(
                actor._parent_chan,
                err,
            )

        # always!
        match err:
            case ContextCancelled():
                log.cancel(
                    f'Actor: {actor.uid} was task-context-cancelled with,\n'
                    f'str(err)'
                )
            case _:
                log.exception("Actor errored:")
        raise

    finally:
        log.runtime(
            'Runtime nursery complete'
            '-> Closing all actor lifetime contexts..'
        )
        # tear down all lifetime contexts if not in guest mode
        # XXX: should this just be in the entrypoint?
        actor.lifetime_stack.close()

        # TODO: we can't actually do this bc the debugger
        # uses the _service_n to spawn the lock task, BUT,
        # in theory if we had the root nursery surround this finally
        # block it might be actually possible to debug THIS
        # machinery in the same way as user task code?
        # if actor.name == 'brokerd.ib':
        #     with CancelScope(shield=True):
        #         await _debug.breakpoint()

        # Unregister actor from the registry-sys / registrar.
        if (
            registered_with_arbiter
            and not actor.is_arbiter
        ):
            failed = False
            assert isinstance(actor._arb_addr, tuple)
            with trio.move_on_after(0.5) as cs:
                cs.shield = True
                try:
                    async with get_arbiter(*actor._arb_addr) as arb_portal:
                        await arb_portal.run_from_ns(
                            'self',
                            'unregister_actor',
                            uid=actor.uid
                        )
                except OSError:
                    failed = True
            if cs.cancelled_caught:
                failed = True
            if failed:
                log.warning(
                    f"Failed to unregister {actor.name} from arbiter")

        # Ensure all peers (actors connected to us as clients) are finished
        if not actor._no_more_peers.is_set():
            if any(
                chan.connected() for chan in chain(*actor._peers.values())
            ):
                log.runtime(
                    f"Waiting for remaining peers {actor._peers} to clear")
                with CancelScope(shield=True):
                    await actor._no_more_peers.wait()
        log.runtime("All peer channels are complete")

    log.runtime("Runtime completed")


async def process_messages(
    actor: Actor,
    chan: Channel,
    shield: bool = False,
    task_status: TaskStatus[CancelScope] = trio.TASK_STATUS_IGNORED,

) -> bool:
    '''
    This is the per-channel, low level RPC task scheduler loop.

    Receive multiplexed RPC request messages from some remote process,
    spawn handler tasks depending on request type and deliver responses
    or boxed errors back to the remote caller (task).

    '''
    # TODO: once `trio` get's an "obvious way" for req/resp we
    # should use it?
    # https://github.com/python-trio/trio/issues/467
    log.runtime(
        'Entering IPC msg loop:\n'
        f'peer: {chan.uid}\n'
        f'|_{chan}\n'
    )
    nursery_cancelled_before_task: bool = False
    msg: dict | None = None
    try:
        # NOTE: this internal scope allows for keeping this
        # message loop running despite the current task having
        # been cancelled (eg. `open_portal()` may call this method
        # from a locally spawned task) and recieve this scope
        # using ``scope = Nursery.start()``
        with CancelScope(shield=shield) as loop_cs:
            task_status.started(loop_cs)
            async for msg in chan:

                # dedicated loop terminate sentinel
                if msg is None:

                    tasks: dict[
                        tuple[Channel, str],
                        tuple[Context, Callable, trio.Event]
                    ] = actor._rpc_tasks.copy()
                    log.cancel(
                        f'Peer IPC channel terminated via `None` setinel msg?\n'
                        f'=> Cancelling all {len(tasks)} local RPC tasks..\n'
                        f'peer: {chan.uid}\n'
                        f'|_{chan}\n'
                    )
                    for (channel, cid) in tasks:
                        if channel is chan:
                            await actor._cancel_task(
                                cid,
                                channel,
                                requesting_uid=channel.uid,

                                ipc_msg=msg,
                            )
                    break

                log.transport(   # type: ignore
                    f'<= IPC msg from peer: {chan.uid}\n\n'

                    # TODO: conditionally avoid fmting depending
                    # on log level (for perf)?
                    # => specifically `pformat()` sub-call..?
                    f'{pformat(msg)}\n'
                )

                cid = msg.get('cid')
                if cid:
                    # deliver response to local caller/waiter
                    # via its per-remote-context memory channel.
                    await actor._push_result(
                        chan,
                        cid,
                        msg,
                    )

                    log.runtime(
                        'Waiting on next IPC msg from\n'
                        f'peer: {chan.uid}:\n'
                        f'|_{chan}\n'

                        # f'last msg: {msg}\n'
                    )
                    continue

                # process a 'cmd' request-msg upack
                # TODO: impl with native `msgspec.Struct` support !!
                # -[ ] implement with ``match:`` syntax?
                # -[ ] discard un-authed msgs as per,
                # <TODO put issue for typed msging structs>
                try:
                    (
                        ns,
                        funcname,
                        kwargs,
                        actorid,
                        cid,
                    ) = msg['cmd']

                except KeyError:
                    # This is the non-rpc error case, that is, an
                    # error **not** raised inside a call to ``_invoke()``
                    # (i.e. no cid was provided in the msg - see above).
                    # Push this error to all local channel consumers
                    # (normally portals) by marking the channel as errored
                    assert chan.uid
                    exc = unpack_error(msg, chan=chan)
                    chan._exc = exc
                    raise exc

                log.runtime(
                    'Handling RPC cmd from\n'
                    f'peer: {actorid}\n'
                    '\n'
                    f'=> {ns}.{funcname}({kwargs})\n'
                )
                if ns == 'self':
                    if funcname == 'cancel':
                        func: Callable = actor.cancel
                        kwargs |= {
                            'req_chan': chan,
                        }

                        # don't start entire actor runtime cancellation
                        # if this actor is currently in debug mode!
                        pdb_complete: trio.Event|None = _debug.Lock.local_pdb_complete
                        if pdb_complete:
                            await pdb_complete.wait()

                        # Either of  `Actor.cancel()`/`.cancel_soon()`
                        # was called, so terminate this IPC msg
                        # loop, exit back out into `async_main()`,
                        # and immediately start the core runtime
                        # machinery shutdown!
                        with CancelScope(shield=True):
                            await _invoke(
                                actor,
                                cid,
                                chan,
                                func,
                                kwargs,
                                is_rpc=False,
                            )

                        log.runtime(
                            'Cancelling IPC transport msg-loop with peer:\n'
                            f'|_{chan}\n'
                        )
                        loop_cs.cancel()
                        break

                    if funcname == '_cancel_task':
                        func: Callable = actor._cancel_task

                        # we immediately start the runtime machinery
                        # shutdown
                        # with CancelScope(shield=True):
                        target_cid: str = kwargs['cid']
                        kwargs |= {
                            # NOTE: ONLY the rpc-task-owning
                            # parent IPC channel should be able to
                            # cancel it!
                            'parent_chan': chan,
                            'requesting_uid': chan.uid,
                            'ipc_msg': msg,
                        }
                        # TODO: remove? already have emit in meth.
                        # log.runtime(
                        #     f'Rx RPC task cancel request\n'
                        #     f'<= canceller: {chan.uid}\n'
                        #     f'  |_{chan}\n\n'
                        #     f'=> {actor}\n'
                        #     f'  |_cid: {target_cid}\n'
                        # )
                        try:
                            await _invoke(
                                actor,
                                cid,
                                chan,
                                func,
                                kwargs,
                                is_rpc=False,
                            )
                        except BaseException:
                            log.exception(
                                'Failed to cancel task?\n'
                                f'<= canceller: {chan.uid}\n'
                                f'  |_{chan}\n\n'
                                f'=> {actor}\n'
                                f'  |_cid: {target_cid}\n'
                            )
                        continue
                    else:
                        # normally registry methods, eg.
                        # ``.register_actor()`` etc.
                        func: Callable = getattr(actor, funcname)

                else:
                    # complain to client about restricted modules
                    try:
                        func = actor._get_rpc_func(ns, funcname)
                    except (ModuleNotExposed, AttributeError) as err:
                        err_msg: dict[str, dict] = pack_error(
                            err,
                            cid=cid,
                        )
                        await chan.send(err_msg)
                        continue

                # schedule a task for the requested RPC function
                # in the actor's main "service nursery".
                # TODO: possibly a service-tn per IPC channel for
                # supervision isolation? would avoid having to
                # manage RPC tasks individually in `._rpc_tasks`
                # table?
                log.runtime(
                    f'Spawning task for RPC request\n'
                    f'<= caller: {chan.uid}\n'
                    f'  |_{chan}\n\n'
                    # TODO: maddr style repr?
                    # f'  |_@ /ipv4/{chan.raddr}/tcp/{chan.rport}/'
                    # f'cid="{cid[-16:]} .."\n\n'

                    f'=> {actor}\n'
                    f'  |_cid: {cid}\n'
                    f'   |>> {func}()\n'
                )
                assert actor._service_n  # wait why? do it at top?
                try:
                    ctx: Context = await actor._service_n.start(
                        partial(
                            _invoke,
                            actor,
                            cid,
                            chan,
                            func,
                            kwargs,
                        ),
                        name=funcname,
                    )

                except (
                    RuntimeError,
                    BaseExceptionGroup,
                ):
                    # avoid reporting a benign race condition
                    # during actor runtime teardown.
                    nursery_cancelled_before_task: bool = True
                    break

                # in the lone case where a ``Context`` is not
                # delivered, it's likely going to be a locally
                # scoped exception from ``_invoke()`` itself.
                if isinstance(err := ctx, Exception):
                    log.warning(
                        'Task for RPC failed?'
                        f'|_ {func}()\n\n'

                        f'{err}'
                    )
                    continue

                else:
                    # mark that we have ongoing rpc tasks
                    actor._ongoing_rpc_tasks = trio.Event()

                    # store cancel scope such that the rpc task can be
                    # cancelled gracefully if requested
                    actor._rpc_tasks[(chan, cid)] = (
                        ctx,
                        func,
                        trio.Event(),
                    )

                log.runtime(
                    'Waiting on next IPC msg from\n'
                    f'peer: {chan.uid}\n'
                    f'|_{chan}\n'
                )

            # end of async for, channel disconnect vis
            # ``trio.EndOfChannel``
            log.runtime(
                f"{chan} for {chan.uid} disconnected, cancelling tasks"
            )
            await actor.cancel_rpc_tasks(
                req_uid=actor.uid,
                # a "self cancel" in terms of the lifetime of the
                # IPC connection which is presumed to be the
                # source of any requests for spawned tasks.
                parent_chan=chan,
            )

    except (
        TransportClosed,
    ):
        # channels "breaking" (for TCP streams by EOF or 104
        # connection-reset) is ok since we don't have a teardown
        # handshake for them (yet) and instead we simply bail out of
        # the message loop and expect the teardown sequence to clean
        # up.
        # TODO: don't show this msg if it's an emphemeral
        # discovery ep call?
        log.runtime(
            f'channel closed abruptly with\n'
            f'peer: {chan.uid}\n' 
            f'|_{chan.raddr}\n'
        )

        # transport **was** disconnected
        return True

    except (
        Exception,
        BaseExceptionGroup,
    ) as err:

        if nursery_cancelled_before_task:
            sn: Nursery = actor._service_n
            assert sn and sn.cancel_scope.cancel_called  # sanity
            log.cancel(
                f'Service nursery cancelled before it handled {funcname}'
            )
        else:
            # ship any "internal" exception (i.e. one from internal
            # machinery not from an rpc task) to parent
            match err:
                case ContextCancelled():
                    log.cancel(
                        f'Actor: {actor.uid} was context-cancelled with,\n'
                        f'str(err)'
                    )
                case _:
                    log.exception("Actor errored:")

            if actor._parent_chan:
                await try_ship_error_to_remote(
                    actor._parent_chan,
                    err,
                )

        # if this is the `MainProcess` we expect the error broadcasting
        # above to trigger an error at consuming portal "checkpoints"
        raise

    finally:
        # msg debugging for when he machinery is brokey
        log.runtime(
            'Exiting IPC msg loop with\n'
            f'peer: {chan.uid}\n'
            f'|_{chan}\n\n'
            'final msg:\n'
            f'{pformat(msg)}\n'
        )

    # transport **was not** disconnected
    return False


class Arbiter(Actor):
    '''
    A special actor who knows all the other actors and always has
    access to a top level nursery.

    The arbiter is by default the first actor spawned on each host
    and is responsible for keeping track of all other actors for
    coordination purposes. If a new main process is launched and an
    arbiter is already running that arbiter will be used.

    '''
    is_arbiter = True

    def __init__(self, *args, **kwargs) -> None:

        self._registry: dict[
            tuple[str, str],
            tuple[str, int],
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

    ) -> tuple[str, int] | None:

        for uid, sockaddr in self._registry.items():
            if name in uid:
                return sockaddr

        return None

    async def get_registry(
        self

    ) -> dict[str, tuple[str, int]]:
        '''
        Return current name registry.

        This method is async to allow for cross-actor invocation.

        '''
        # NOTE: requires ``strict_map_key=False`` to the msgpack
        # unpacker since we have tuples as keys (not this makes the
        # arbiter suscetible to hashdos):
        # https://github.com/msgpack/msgpack-python#major-breaking-changes-in-msgpack-10
        return {'.'.join(key): val for key, val in self._registry.items()}

    async def wait_for_actor(
        self,
        name: str,

    ) -> list[tuple[str, int]]:
        '''
        Wait for a particular actor to register.

        This is a blocking call if no actor by the provided name is currently
        registered.

        '''
        sockaddrs: list[tuple[str, int]] = []
        sockaddr: tuple[str, int]

        for (aname, _), sockaddr in self._registry.items():
            log.runtime(
                f'Actor mailbox info:\n'
                f'aname: {aname}\n'
                f'sockaddr: {sockaddr}\n'
            )
            if name == aname:
                sockaddrs.append(sockaddr)

        if not sockaddrs:
            waiter = trio.Event()
            self._waiters.setdefault(name, []).append(waiter)
            await waiter.wait()

            for uid in self._waiters[name]:
                if not isinstance(uid, trio.Event):
                    sockaddrs.append(self._registry[uid])

        return sockaddrs

    async def register_actor(
        self,
        uid: tuple[str, str],
        sockaddr: tuple[str, int]

    ) -> None:
        uid = name, _ = (str(uid[0]), str(uid[1]))
        self._registry[uid] = (str(sockaddr[0]), int(sockaddr[1]))

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
        self._registry.pop(uid)
