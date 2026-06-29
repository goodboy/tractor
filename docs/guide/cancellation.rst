Cancellation and error propagation
==================================

``tractor`` supports ``trio``'s cancellation_ system *verbatim*,
then extends it across process boundaries. If you know how to
cancel a task in ``trio`` you already know how to cancel an actor —
and its whole subtree — in ``tractor``; the runtime's job is making
that statement hold over IPC with every structured concurrency (SC)
guarantee intact.

The ground rules,

- a remote actor is **never** cancelled unless explicitly requested
  (by a parent or peer), unless supervision demands it (an error
  triggered one-cancels-all teardown), or unless there's a bug in
  ``tractor`` itself (please report it!),
- (remote) errors `always propagate`_ back to the parent
  supervisor; nothing is silently dropped on the floor,
- every spawned process gets reaped no matter how it dies; if you
  can create a zombie child process (without using a system signal)
  it **is a bug**.

``trio`` cancellation, across the wire
--------------------------------------

Locally everything is bog-standard ``trio``: nurseries, cancel
scopes, timeouts. ``tractor`` adds exactly one twist: a cancel
scope can't physically reach into another process, so the runtime
*relays cancellation as messages*. Concretely,

- cancelling an *actor* means sending it a runtime-cancel request
  msg; the target then runs its own graceful teardown — cancelling
  RPC tasks, closing channels, exiting its :func:`trio.run` — and
  acks the request back to the canceller,
- cancelling a single *cross-actor task* works through the
  :class:`tractor.Context` layer: each ``ctx`` task-pair is
  cancel-scope-linked over IPC such that either side erroring or
  cancelling relays an equivalent error to the other side (see
  :doc:`/guide/context` for the gory details),
- a cancel is therefore always a *request with an ack*: the
  canceller does a **bounded wait** for confirmation and escalates
  if the peer is unresponsive (see the teardown ladder below).

One-cancels-all supervision
---------------------------

An :class:`tractor.ActorNursery` supervises subactors `exactly like
trio`_ nurseries supervise tasks: when one child errors, the error
propagates to the supervising block and **all** sibling subactors
get cancelled before the error continues bubbling up the (process)
tree.

.. d2:: diagrams/error_propagation.d2
   :caption: One-cancels-all: no zombies, no lost errors.
   :alt: error propagation up a subactor tree
   :width: 80%

.. literalinclude:: ../../examples/remote_error_propagation.py
   :caption: examples/remote_error_propagation.py
   :language: python

What's going on here?

- three healthy actors are spawned as daemons via
  :meth:`tractor.ActorNursery.start_actor`; left alone they'd
  happily idle forever,
- a fourth actor runs ``assert_err()`` via ``.run_in_actor()`` and
  promptly trips its ``assert 0``,
- the resulting ``AssertionError`` ships back over IPC as a
  serialized error msg and re-raises *boxed* inside the nursery
  block as a :class:`tractor.RemoteActorError`,
- the nursery reacts like any ``trio`` nursery would: it cancels
  the three healthy siblings (graceful runtime-cancel requests,
  acks awaited), reaps all four processes, then re-raises,
- ``trio.run(main)`` sees that same ``RemoteActorError`` in the
  parent-most process — propagation is end-to-end or bust.

This one-cancels-all style is currently the *only* supervision
strategy offered (it's the one ``trio`` gives you); more
`erlang strategies`_ are roadmap, see the bottom of this page.

The boxed-error bestiary
------------------------

All remote failures arrive locally as one of a small set of
exception types, each carrying enough metadata to work out *who*
failed, *where*, and *why*.

``RemoteActorError``
********************

The workhorse: a "boxed" exception relayed over IPC from another
actor. The original error's type, traceback string and msgdata are
preserved so you can pattern-match on what actually went wrong
remotely,

- ``.boxed_type``: the reconstructed **type** of the original
  remote exception (``ValueError``, ``NameError``, what have you),
- ``.src_uid``: the ``(name, uuid)`` pair of the actor where the
  error *originated*,
- ``.relay_uid`` / ``.relay_path``: when an error crosses more than
  one actor boundary (grandchild -> child -> root) every relaying
  actor is recorded; multi-hop boxings are lovingly referred to as
  "inceptions" in the runtime internals,
- ``.pformat()``: a rich "tb box" rendering of the remote traceback
  for your logs or REPL.

.. code:: python

    try:
        async with portal.open_context(ep) as (ctx, first):
            ...
    except tractor.RemoteActorError as rae:
        if rae.boxed_type is ValueError:
            ...  # the remote task raised `ValueError`

``ContextCancelled``
********************

The cancel-ack for a cross-actor task pair: raised when a
:class:`tractor.Context` task is cancelled *by request*. Its
``.canceller`` attr is the uid of the actor which **requested** the
cancel, which powers the key rule,

- if **you** requested it (you called
  :meth:`tractor.Context.cancel`) the resulting ctxc is *absorbed*
  at ``open_context()`` exit: an expected outcome, not an error,
- if **anyone else** did — the peer task, or some third-party actor
  — it *raises* locally so your code always hears about it.

The full self- vs. cross-cancel semantics are a core teaching point
of :doc:`/guide/context`; go read them there.

``MsgTypeError``
****************

An IPC-payload "type error": a msg violated the dialog's declared
payload spec. See :doc:`/guide/msging` for the typed-messaging
system which enforces it.

``TransportClosed``
*******************

The underlying IPC transport (TCP stream, UDS socket, ...) died or
closed out from under a channel. You'll normally only see this
surface when a peer hard-exits without any graceful runtime
teardown; the supervision machinery treats unexpected transport
loss on a busy channel as a failure and tears down accordingly.

Pick your blast radius
----------------------

Three cancel surfaces, three scopes of effect; choose the smallest
hammer that does the job.

.. list-table::
   :header-rows: 1
   :widths: 36 34 30

   * - surface
     - cancels
     - typical use
   * - :meth:`tractor.ActorNursery.cancel`
     - every subactor in the nursery
     - whole-tree teardown
   * - :meth:`tractor.Portal.cancel_actor`
     - one actor: full runtime + proc
     - daemon teardown
   * - :meth:`tractor.Context.cancel`
     - exactly one remote task
     - surgical task cancel

``ActorNursery.cancel()``
*************************

The big red button: gracefully cancel every subactor supervised by
the nursery, in parallel, with the escalation discipline below
applied per-child. It's invoked for you whenever an error hits the
nursery block (one-cancels-all); call it yourself for an orderly
early shutdown. Passing ``hard_kill=True`` skips the graceful phase
and goes straight to OS-level process termination — rarely what you
want outside tests.

``Portal.cancel_actor()``
*************************

Cancel one **whole actor**: its entire runtime, every task it's
scheduled, and (for subactors) the OS process, via a graceful
runtime-cancel request,

.. code:: python

    await portal.cancel_actor()    # bounded wait, bool result
    await portal.cancel_actor(
        raise_on_timeout=True,     # no ack in time?
    )                              # -> `ActorTooSlowError`

The wait for the peer's ack is *bounded* (default
``Portal.cancel_timeout = 0.5`` seconds, tunable per call via
``timeout=``). By default a missed ack just returns ``False``; with
``raise_on_timeout=True`` you instead get an ``ActorTooSlowError``
(from ``tractor._exceptions``) so *your* code can escalate per SC
discipline — exactly what the nursery's own teardown does
internally before resorting to OS-level signalling.

Note the granularity: this cancels an **actor**, not a task. For
one remote task use the ``Context`` layer instead.

``Context.cancel()``
********************

Request cancellation of exactly one remote task: the peer task of
an open :class:`tractor.Context`. Two things to keep straight,

- it cancels the **remote** side only; a ``Context`` is *not* a
  :class:`trio.CancelScope` and your local task keeps running until
  you exit the ``open_context()`` block,
- the resulting :class:`tractor.ContextCancelled` is absorbed
  locally (you asked for it, after all) per the self- vs.
  cross-cancel rule above.

Again, :doc:`/guide/context` covers this dance in depth.

Graceful first, hard as a last resort
-------------------------------------

.. margin:: REPL-safe by design

   The hard-kill path is *skipped* whenever an actor in the tree
   holds the debug-REPL lock (``debug_mode=True`` flavors):
   SIGTERM raining down on a tree mid-``pdb`` session would
   clobber your prompt. See :doc:`/guide/debugging`.

Every process teardown in ``tractor`` walks the same escalation
ladder, top rung first,

1. **graceful cancel request**: a runtime-cancel msg over IPC; the
   target actor cancels its tasks, closes its channels and exits
   its :func:`trio.run` cleanly,
2. **soft wait**: the parent waits (bounded) for the child process
   to exit on its own,
3. **SIGTERM**: no ack within the bounded wait (internally an
   ``ActorTooSlowError``) escalates to ``proc.terminate()``,
4. **SIGKILL ultimatum**: still alive after the hard-kill timeout
   (~1.6s)? The runtime logs that the "T-800" has been deployed to
   collect the zombie and issues ``proc.kill()``. No survivors.

The result is the **no-zombies guarantee**: ``tractor`` tries to
protect you from zombies, no matter what. Quoting the project
manifesto,

    If you can create zombie child processes (without using
    a system signal) it **is a bug**.

Run the quickstart's self-destructing process-tree demo
(``examples/parallelism/we_are_processes.py``, walked through in
:doc:`/start/quickstart`) under a ``pstree`` watcher and try to
catch a
straggler; we'll wait B)

Roadmap: ``erlang``-style strategies
------------------------------------

One-cancels-all is ``trio``'s strategy and, for now, the only one
``tractor`` ships. Pluggable `erlang strategies`_ — one-for-one
restarts, rest-for-one, transient/permanent child specs and friends
(see the `supervision strategies`_ canon) — are a long-standing
roadmap item tracked in `#22`_. If supervisors are your jam that
issue is the place to sling opinions.

.. seealso::

   - :doc:`/guide/context` — the cross-actor task layer where
     per-task cancellation actually lives,
   - :doc:`/guide/msging` — the typed msg layer that raises
     :class:`tractor.MsgTypeError`,
   - :doc:`/guide/debugging` — what cancellation does (and very
     carefully does *not* do) while a REPL is up.

.. _cancellation: https://trio.readthedocs.io/en/latest/reference-core.html#cancellation-and-timeouts
.. _exactly like trio: https://trio.readthedocs.io/en/latest/reference-core.html#cancellation-semantics
.. _always propagate: https://trio.readthedocs.io/en/latest/design.html#exceptions-always-propagate
.. _erlang strategies: https://learnyousomeerlang.com/supervisors
.. _supervision strategies: https://www.erlang.org/doc/system/sup_princ.html
.. _#22: https://github.com/goodboy/tractor/issues/22
