Errors and cancellation types
=============================

``tractor`` extends trio's "exceptions `always propagate`_" rule
across the process boundary: a crash in any actor is serialized as
an ``Error`` msg, shuttled over IPC, and re-raised in the linked
parent scope as a *boxed* :class:`RemoteActorError` — preserving
the original type, traceback text and source-actor identity, even
across multi-hop relays (a.k.a. "inceptions").

The most-used types below are importable from ``tractor``
directly; the remainder live in ``tractor._exceptions`` (not yet
re-exported at top level).

.. currentmodule:: tractor

Boxed remote errors
-------------------

.. autoexception:: RemoteActorError
   :members: boxed_type,
             src_uid,
             relay_uid,
             pformat

.. code:: python

   try:
       async with portal.open_context(ep_fn) as (ctx, first):
           ...
   except tractor.RemoteActorError as rae:
       if rae.boxed_type is ValueError:
           ...  # remote task raised a `ValueError`

.. autoexception:: ContextCancelled
   :show-inheritance:
   :members: canceller

.. note::

   Inspect :attr:`ContextCancelled.canceller` (the requesting
   actor's uid) to distinguish a *self*-requested cancel (absorbed
   at ``open_context()`` exit) from a *cross*-actor cancel (raised
   locally) — the full rules live in :doc:`/api/context`.

Typed-messaging errors
----------------------

.. autoexception:: MsgTypeError
   :show-inheritance:
   :members: bad_msg,
             expected_msg_type

An "IPC ``TypeError``": a message failed validation against the
active msg-spec / ``pld_spec`` (see :doc:`/api/msg`). Raised
sender-side for control msgs (``Started``/``Return``) and
receiver-side for stream ``Yield`` payloads.

.. autoexception:: tractor._exceptions.StreamOverrun
   :show-inheritance:

The sender out-paced the receiver's buffer on a
:class:`~tractor.MsgStream` opened without
``allow_overruns=True``; subtypes :class:`trio.TooSlowError`.

Transport and runtime errors
----------------------------

.. autoexception:: TransportClosed

.. autoexception:: ModuleNotExposed
   :show-inheritance:

Raised when an RPC requests a function from a module not listed
in the target actor's ``enable_modules`` allowlist —
capability-style access control, not an import bug on your end ;)

.. autoexception:: tractor._exceptions.NoRuntime
   :show-inheritance:

Raised by :func:`tractor.current_actor` (and friends) when no
actor runtime is up in the current process.

.. autoexception:: tractor._exceptions.ActorTooSlowError
   :show-inheritance:

A peer actor failed to ack a cancel request within the bounded
wait — the SC-sanctioned escalation signal from APIs like
``Portal.cancel_actor(raise_on_timeout=True)``. Catch it to
escalate (e.g. hard-kill via the supervising
:class:`~tractor.ActorNursery`); never just ignore it, that's how
zombies happen.

.. seealso::

   :doc:`/api/context` for how cancellation and errors flow
   through a :class:`~tractor.Context`, :doc:`/api/devx` for
   crash-handling REPL tooling (``debug_mode``, post-mortems),
   and :doc:`/guide/cancellation` for the full SC-cancellation
   story.

.. _always propagate: https://trio.readthedocs.io/en/latest/design.html#exceptions-always-propagate
