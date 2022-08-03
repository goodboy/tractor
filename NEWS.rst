=========
Changelog
=========

.. towncrier release notes start

tractor 0.1.0a5 (2022-08-03)
============================

This is our final release supporting Python 3.9 since we will be moving
internals to the new `match:` syntax from 3.10 going forward and
further, we have officially dropped usage of the `msgpack` library and
happily adopted `msgspec`.

Features
--------

- `#165 <https://github.com/goodboy/tractor/issues/165>`_: Add SIGINT
  protection to our `pdbpp` based debugger subystem such that for
  (single-depth) actor trees in debug mode we ignore interrupts in any
  actor currently holding the TTY lock thus avoiding clobbering IPC
  connections and/or task and process state when working in the REPL.

  As a big note currently so called "nested" actor trees (trees with
  actors having more then one parent/ancestor) are not fully supported
  since we don't yet have a mechanism to relay the debug mode knowledge
  "up" the actor tree (for eg. when handling a crash in a leaf actor).
  As such currently there is a set of tests and known scenarios which will
  result in process cloberring by the zombie repaing machinery and these
  have been documented in https://github.com/goodboy/tractor/issues/320.

  The implementation details include:

  - utilizing a custom SIGINT handler which we apply whenever an actor's
    runtime enters the debug machinery, which we also make sure the
    stdlib's `pdb` configuration doesn't override (which it does by
    default without special instance config).
  - litter the runtime with `maybe_wait_for_debugger()` mostly in spots
    where the root actor should block before doing embedded nursery
    teardown ops which both cancel potential-children-in-deubg as well
    as eventually trigger zombie reaping machinery.
  - hardening of the TTY locking semantics/API both in terms of IPC
    terminations and cancellation and lock release determinism from
    sync debugger instance methods.
  - factoring of locking infrastructure into a new `._debug.Lock` global
    which encapsulates all details of the ``trio`` sync primitives and
    task/actor uid management and tracking.

  We also add `ctrl-c` cases throughout the test suite though these are
  disabled for py3.9 (`pdbpp` UX differences that don't seem worth
  compensating for, especially since this will be our last 3.9 supported
  release) and there are a slew of marked cases that aren't expected to
  work in CI more generally (as mentioned in the "nested" tree note
  above) despite seemingly working  when run manually on linux.

- `#304 <https://github.com/goodboy/tractor/issues/304>`_: Add a new
  ``to_asyncio.LinkedTaskChannel.subscribe()`` which gives task-oriented
  broadcast functionality semantically equivalent to
  ``tractor.MsgStream.subscribe()`` this makes it possible for multiple
  ``trio``-side tasks to consume ``asyncio``-side task msgs in tandem.

  Further Improvements to the test suite were added in this patch set
  including a new scenario test for a sub-actor managed "service nursery"
  (implementing the basics of a "service manager") including use of
  *infected asyncio* mode. Further we added a lower level
  ``test_trioisms.py`` to start to track issues we need to work around in
  ``trio`` itself which in this case included a bug we were trying to
  solve related to https://github.com/python-trio/trio/issues/2258.


Bug Fixes
---------

- `#318 <https://github.com/goodboy/tractor/issues/318>`_: Fix
  a previously undetected ``trio``-``asyncio`` task lifetime linking
  issue with the ``to_asyncio.open_channel_from()`` api where both sides
  where not properly waiting/signalling termination and it was possible
  for ``asyncio``-side errors to not propagate due to a race condition.

  The implementation fix summary is:
  - add state to signal the end of the ``trio`` side task to be
    read by the ``asyncio`` side and always cancel any ongoing
    task in such cases.
  - always wait on the ``asyncio`` task termination from the ``trio``
    side on error before maybe raising said error.
  - always close the ``trio`` mem chan on exit to ensure the other
    side can detect it and follow.


Trivial/Internal Changes
------------------------

- `#248 <https://github.com/goodboy/tractor/issues/248>`_: Adjust the
  `tractor._spawn.soft_wait()` strategy to avoid sending an actor cancel
  request (via `Portal.cancel_actor()`) if either the child process is
  detected as having terminated or the IPC channel is detected to be
  closed.

  This ensures (even) more deterministic inter-actor cancellation by
  avoiding the timeout condition where possible when a whild never
  sucessfully spawned, crashed, or became un-contactable over IPC.

- `#295 <https://github.com/goodboy/tractor/issues/295>`_: Add an
  experimental ``tractor.msg.NamespacePath`` type for passing Python
  objects by "reference" through a ``str``-subtype message and using the
  new ``pkgutil.resolve_name()`` for reference loading.

- `#298 <https://github.com/goodboy/tractor/issues/298>`_: Add a new
  `tractor.experimental` subpackage for staging new high level APIs and
  subystems that we might eventually make built-ins.

- `#300 <https://github.com/goodboy/tractor/issues/300>`_: Update to and
  pin latest ``msgpack`` (1.0.3) and ``msgspec`` (0.4.0) both of which
  required adjustments for backwards imcompatible API tweaks.

- `#303 <https://github.com/goodboy/tractor/issues/303>`_: Fence off
  ``multiprocessing`` imports until absolutely necessary in an effort to
  avoid "resource tracker" spawning side effects that seem to have
  varying degrees of unreliability per Python release. Port to new
  ``msgspec.DecodeError``.

- `#305 <https://github.com/goodboy/tractor/issues/305>`_: Add
  ``tractor.query_actor()`` an addr looker-upper which doesn't deliver
  a ``Portal`` instance and instead just a socket address ``tuple``.

  Sometimes it's handy to just have a simple way to figure out if
  a "service" actor is up, so add this discovery helper for that. We'll
  prolly just leave it undocumented for now until we figure out
  a longer-term/better discovery system.

- `#316 <https://github.com/goodboy/tractor/issues/316>`_: Run windows
  CI jobs on python 3.10 after some hacks for ``pdbpp`` dependency
  issues.

  Issue was to do with the now deprecated `pyreadline` project which
  should be changed over to `pyreadline3`.

- `#317 <https://github.com/goodboy/tractor/issues/317>`_: Drop use of
  the ``msgpack`` package and instead move fully to the ``msgspec``
  codec library.

  We've now used ``msgspec`` extensively in production and there's no
  reason to not use it as default. Further this change preps us for the up
  and coming typed messaging semantics (#196), dialog-unprotocol system
  (#297), and caps-based messaging-protocols (#299) planned before our
  first beta.


tractor 0.1.0a4 (2021-12-18)
============================

Features
--------
- `#275 <https://github.com/goodboy/tractor/issues/275>`_: Re-license
  code base under AGPLv3. Also see `#274
  <https://github.com/goodboy/tractor/pull/274>`_ for majority
  contributor consensus on this decision.

- `#121 <https://github.com/goodboy/tractor/issues/121>`_: Add
  "infected ``asyncio`` mode; a sub-system to spawn and control
  ``asyncio`` actors using ``trio``'s guest-mode.

  This gets us the following very interesting functionality:

  - ability to spawn an actor that has a process entry point of
    ``asyncio.run()`` by passing ``infect_asyncio=True`` to
    ``Portal.start_actor()`` (and friends).
  - the ``asyncio`` actor embeds ``trio`` using guest-mode and starts
    a main ``trio`` task which runs the ``tractor.Actor._async_main()``
    entry point engages all the normal ``tractor`` runtime IPC/messaging
    machinery; for all purposes the actor is now running normally on
    a ``trio.run()``.
  - the actor can now make one-to-one task spawning requests to the
    underlying ``asyncio`` event loop using either of:

    * ``to_asyncio.run_task()`` to spawn and run an ``asyncio`` task to
      completion and block until a return value is delivered.
    * ``async with to_asyncio.open_channel_from():`` which spawns a task
      and hands it a pair of "memory channels" to allow for bi-directional
      streaming between the now SC-linked ``trio`` and ``asyncio`` tasks.

  The output from any call(s) to ``asyncio`` can be handled as normal in
  ``trio``/``tractor`` task operation with the caveat of the overhead due
  to guest-mode use.

  For more details see the `original PR
  <https://github.com/goodboy/tractor/pull/121>`_ and `issue
  <https://github.com/goodboy/tractor/issues/120>`_.

- `#257 <https://github.com/goodboy/tractor/issues/257>`_: Add
  ``trionics.maybe_open_context()`` an actor-scoped async multi-task
  context manager resource caching API.

  Adds an SC-safe cacheing async context manager api that only enters on
  the *first* task entry and only exits on the *last* task exit while in
  between delivering the same cached value per input key. Keys can be
  either an explicit ``key`` named arg provided by the user or a
  hashable ``kwargs`` dict (will be converted to a ``list[tuple]``) which
  is passed to the underlying manager function as input.

- `#261 <https://github.com/goodboy/tractor/issues/261>`_: Add
  cross-actor-task ``Context`` oriented error relay, a new stream
  overrun error-signal ``StreamOverrun``, and support disabling
  ``MsgStream`` backpressure as the default before a stream is opened or
  by choice of the user.

  We added stricter semantics around ``tractor.Context.open_stream():``
  particularly to do with streams which are only opened at one end.
  Previously, if only one end opened a stream there was no way for that
  sender to know if msgs are being received until first, the feeder mem
  chan on the receiver side hit a backpressure state and then that
  condition delayed its msg loop processing task to eventually create
  backpressure on the associated IPC transport. This is non-ideal in the
  case where the receiver side never opened a stream by mistake since it
  results in silent block of the sender and no adherence to the underlying
  mem chan buffer size settings (which is still unsolved btw).

  To solve this we add non-backpressure style message pushing inside
  ``Actor._push_result()`` by default and only use the backpressure
  ``trio.MemorySendChannel.send()`` call **iff** the local end of the
  context has entered ``Context.open_stream():``. This way if the stream
  was never opened but the mem chan is overrun, we relay back to the
  sender a (new exception) ``SteamOverrun`` error which is raised in the
  sender's scope with a special error message about the stream never
  having been opened. Further, this behaviour (non-backpressure style
  where senders can expect an error on overruns) can now be enabled with
  ``.open_stream(backpressure=False)`` and the underlying mem chan size
  can be specified with a kwarg ``msg_buffer_size: int``.

  Further bug fixes and enhancements in this changeset include:

  - fix a race we were ignoring where if the callee task opened a context
    it could enter ``Context.open_stream()`` before calling
    ``.started()``.
  - Disallow calling ``Context.started()`` more then once.
  - Enable ``Context`` linked tasks error relaying via the new
    ``Context._maybe_raise_from_remote_msg()`` which (for now) uses
    a simple ``trio.Nursery.start_soon()`` to raise the error via closure
    in the local scope.

- `#267 <https://github.com/goodboy/tractor/issues/267>`_: This
  (finally) adds fully acknowledged remote cancellation messaging
  support for both explicit ``Portal.cancel_actor()`` calls as well as
  when there is a "runtime-wide" cancellations (eg. during KBI or
  general actor nursery exception handling which causes a full actor
  "crash"/termination).

  You can think of this as the most ideal case in 2-generals where the
  actor requesting the cancel of its child is able to always receive back
  the ACK to that request. This leads to a more deterministic shutdown of
  the child where the parent is able to wait for the child to fully
  respond to the request. On a localhost setup, where the parent can
  monitor the state of the child through process or other OS APIs instead
  of solely through IPC messaging, the parent can know whether or not the
  child decided to cancel with more certainty. In the case of separate
  hosts, we still rely on a simple timeout approach until such a time
  where we prefer to get "fancier".

- `#271 <https://github.com/goodboy/tractor/issues/271>`_: Add a per
  actor ``debug_mode: bool`` control to our nursery.

  This allows spawning actors via ``ActorNursery.start_actor()`` (and
  other dependent methods) with a ``debug_mode=True`` flag much like
  ``tractor.open_nursery():`` such that per process crash handling
  can be toggled for cases where a user does not need/want all child actors
  to drop into the debugger on error. This is often useful when you have
  actor-tasks which are expected to error often (and be re-run) but want
  to specifically interact with some (problematic) child.


Bugfixes
--------

- `#239 <https://github.com/goodboy/tractor/issues/239>`_: Fix
  keyboard interrupt handling in ``Portal.open_context()`` blocks.

  Previously this was not triggering cancellation of the remote task
  context and could result in hangs if a stream was also opened. This
  fix is to accept `BaseException` since it is likely any other top
  level exception other then KBI (even though not expected) should also
  get this result.

- `#264 <https://github.com/goodboy/tractor/issues/264>`_: Fix
  ``Portal.run_in_actor()`` returns ``None`` result.

  ``None`` was being used as the cached result flag and obviously breaks
  on a ``None`` returned from the remote target task. This would cause an
  infinite hang if user code ever called ``Portal.result()`` *before* the
  nursery exit. The simple fix is to use the *return message* as the
  initial "no-result-received-yet" flag value and, once received, the
  return value is read from the message to avoid the cache logic error.

- `#266 <https://github.com/goodboy/tractor/issues/266>`_: Fix
  graceful cancellation of daemon actors

  Previously, his was a bug where if the soft wait on a sub-process (the
  ``await .proc.wait()``) in the reaper task teardown was cancelled we
  would fail over to the hard reaping sequence (meant for culling off any
  potential zombies via system kill signals). The hard reap has a timeout
  of 3s (currently though in theory we could make it shorter?) before
  system signalling kicks in. This means that any daemon actor still
  running during nursery exit would get hard reaped (3s later) instead of
  cancelled via IPC message. Now we catch the ``trio.Cancelled``, call
  ``Portal.cancel_actor()`` on the daemon and expect the child to
  self-terminate after the runtime cancels and shuts down the process.

- `#278 <https://github.com/goodboy/tractor/issues/278>`_: Repair
  inter-actor stream closure semantics to work correctly with
  ``tractor.trionics.BroadcastReceiver`` task fan out usage.

  A set of previously unknown bugs discovered in `#257
  <https://github.com/goodboy/tractor/pull/257>`_ let graceful stream
  closure result in hanging consumer tasks that use the broadcast APIs.
  This adds better internal closure state tracking to the broadcast
  receiver and message stream APIs and in particular ensures that when an
  underlying stream/receive-channel (a broadcast receiver is receiving
  from) is closed, all consumer tasks waiting on that underlying channel
  are woken so they can receive the ``trio.EndOfChannel`` signal and
  promptly terminate.


tractor 0.1.0a3 (2021-11-02)
============================

Features
--------

- Switch to using the ``trio`` process spawner by default on windows. (#166)

  This gets windows users debugger support (manually tested) and in
  general a more resilient (nested) actor tree implementation.

- Add optional `msgspec <https://jcristharif.com/msgspec/>`_ support
  as an alernative, faster MessagePack codec. (#214)

  Provides us with a path toward supporting typed IPC message contracts. Further,
  ``msgspec`` structs may be a valid tool to start for formalizing our
  "SC dialog un-protocol" messages as described in `#36
  <https://github.com/goodboy/tractor/issues/36>`_.

- Introduce a new ``tractor.trionics`` `sub-package`_ that exposes
  a selection of our relevant high(er) level trio primitives and
  goodies. (#241)

  At outset we offer a ``gather_contexts()`` context manager for
  concurrently entering a sequence of async context managers (much like
  a version of ``asyncio.gather()`` but for context managers) and use it
  in a new ``tractor.open_actor_cluster()`` manager-helper that can be
  entered to concurrently spawn a flat actor pool. We also now publicly
  expose our "broadcast channel" APIs (``open_broadcast_receiver()``)
  from here.

.. _sub-package: ../tractor/trionics

- Change the core message loop to handle task and actor-runtime cancel
  requests immediately instead of scheduling them as is done for rpc-task
  requests. (#245)

  In order to obtain more reliable teardown mechanics for (complex) actor
  trees it's important that we specially treat cancel requests as having
  higher priority. Previously, it was possible that task cancel requests
  could actually also themselves be cancelled if a "actor-runtime" cancel
  request was received (can happen during messy multi actor crashes that
  propagate). Instead cancels now block the msg loop until serviced and
  a response is relayed back to the requester. This also allows for
  improved debugger support since we have determinism guarantees about
  which processes must wait before hard killing their children.

- (`#248 <https://github.com/goodboy/tractor/pull/248>`_) Drop Python
  3.8 support in favour of rolling with two latest releases for the time
  being.


Misc
----

- (`#243 <https://github.com/goodboy/tractor/pull/243>`_) add a distinct
  ``'CANCEL'`` log level to allow the runtime to emit details about
  cancellation machinery statuses.


tractor 0.1.0a2 (2021-09-07)
============================

Features
--------

- Add `tokio-style broadcast channels
  <https://docs.rs/tokio/1.11.0/tokio/sync/broadcast/index.html>`_ as
  a solution for `#204 <https://github.com/goodboy/tractor/pull/204>`_ and
  discussed thoroughly in `trio/#987
  <https://github.com/python-trio/trio/issues/987>`_.

  This gives us local task broadcast functionality using a new
  ``BroadcastReceiver`` type which can wrap ``trio.ReceiveChannel``  and
  provide fan-out copies of a stream of data to every subscribed consumer.
  We use this new machinery to provide a ``ReceiveMsgStream.subscribe()``
  async context manager which can be used by actor-local concumers tasks
  to easily pull from a shared and dynamic IPC stream. (`#229
  <https://github.com/goodboy/tractor/pull/229>`_)


Bugfixes
--------

- Handle broken channel/stream faults where the root's tty lock is left
  acquired by some child actor who went MIA and the root ends up hanging
  indefinitely. (`#234 <https://github.com/goodboy/tractor/pull/234>`_)

  There's two parts here: we no longer shield wait on the lock and,
  now always do our best to release the lock on the expected worst
  case connection faults.


Deprecations and Removals
-------------------------

- Drop stream "shielding" support which was originally added to sidestep
  a cancelled call to ``.receive()``

  In the original api design a stream instance was returned directly from
  a call to ``Portal.run()`` and thus there was no "exit phase" to handle
  cancellations and errors which would trigger implicit closure. Now that
  we have said enter/exit semantics with ``Portal.open_stream_from()`` and
  ``Context.open_stream()`` we can drop this implicit (and arguably
  confusing) behavior. (`#230 <https://github.com/goodboy/tractor/pull/230>`_)

- Drop Python 3.7 support in preparation for supporting 3.9+ syntax.
  (`#232 <https://github.com/goodboy/tractor/pull/232>`_)


tractor 0.1.0a1 (2021-08-01)
============================

Features
--------
- Updated our uni-directional streaming API (`#206
  <https://github.com/goodboy/tractor/pull/206>`_) to require a context
  manager style ``async with Portal.open_stream_from(target) as stream:``
  which explicitly determines when to stop a stream in the calling (aka
  portal opening) actor much like ``async_generator.aclosing()``
  enforcement.

- Improved the ``multiprocessing`` backend sub-actor reaping (`#208
  <https://github.com/goodboy/tractor/pull/208>`_) during actor nursery
  exit, particularly during cancellation scenarios that previously might
  result in hard to debug hangs.

- Added initial bi-directional streaming support in `#219
  <https://github.com/goodboy/tractor/pull/219>`_ with follow up debugger
  improvements via `#220 <https://github.com/goodboy/tractor/pull/220>`_
  using the new ``tractor.Context`` cross-actor task syncing system.
  The debugger upgrades add an edge triggered last-in-tty-lock semaphore
  which allows the root process for a tree to avoid clobbering children
  who have queued to acquire the ``pdb`` repl by waiting to cancel
  sub-actors until the lock is known to be released **and** has no
  pending waiters.


Experiments and WIPs
--------------------
- Initial optional ``msgspec`` serialization support in `#214
  <https://github.com/goodboy/tractor/pull/214>`_ which should hopefully
  land by next release.

- Improved "infect ``asyncio``" cross-loop task cancellation and error
  propagation by vastly simplifying the cross-loop-task streaming approach. 
  We may end up just going with a use of ``anyio`` in the medium term to
  avoid re-doing work done by their cross-event-loop portals.  See the
  ``infect_asyncio`` for details.


Improved Documentation
----------------------
- `Updated our readme <https://github.com/goodboy/tractor/pull/211>`_ to
  include more (and better) `examples
  <https://github.com/goodboy/tractor#run-a-func-in-a-process>`_ (with
  matching multi-terminal process monitoring shell commands) as well as
  added many more examples to the `repo set
  <https://github.com/goodboy/tractor/tree/master/examples>`_.

- Added a readme `"actors under the hood" section
  <https://github.com/goodboy/tractor#under-the-hood>`_ in an effort to
  guard against suggestions for changing the API away from ``trio``'s
  *tasks-as-functions* style.

- Moved to using the `sphinx book theme
  <https://sphinx-book-theme.readthedocs.io/en/latest/index.html>`_
  though it needs some heavy tweaking and doesn't seem to show our logo
  on rtd :(


Trivial/Internal Changes
------------------------
- Added a new ``TransportClosed`` internal exception/signal (`#215
  <https://github.com/goodboy/tractor/pull/215>`_ for catching TCP
  channel gentle closes instead of silently falling through the message
  handler loop via an async generator ``return``.


Deprecations and Removals
-------------------------
- Dropped support for invoking sync functions (`#205
  <https://github.com/goodboy/tractor/pull/205>`_) in other
  actors/processes since you can always wrap a sync function from an
  async one.  Users can instead consider using ``trio-parallel`` which
  is a project specifically geared for purely synchronous calls in
  sub-processes.

- Deprecated our ``tractor.run()`` entrypoint `#197
  <https://github.com/goodboy/tractor/pull/197>`_; the runtime is now
  either started implicitly in first actor nursery use or via an
  explicit call to ``tractor.open_root_actor()``. Full removal of
  ``tractor.run()`` will come by beta release.


tractor 0.1.0a0 (2021-02-28)
============================

..
    TODO: fill out more of the details of the initial feature set in some TLDR form

Summary
-------
- ``trio`` based process spawner (using ``subprocess``)
- initial multi-process debugging with ``pdb++``
- windows support using both ``trio`` and ``multiprocessing`` spawners
- "portal" api for cross-process, structured concurrent, (streaming) IPC
