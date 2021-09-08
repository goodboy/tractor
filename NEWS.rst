=========
Changelog
=========

.. towncrier release notes start

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
