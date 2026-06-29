Contexts and streaming
======================

The modern core of ``tractor``: a :class:`Context` links a task in
one actor to a task in another as a *single* structured concurrency
(SC) scope stretched across the IPC boundary — errors, results and
cancellation flow between the pair `exactly like trio`_ tasks under
a common `nursery`_. Open one with ``Portal.open_context()`` (see
:func:`tractor._context.open_context_from_portal` in
:doc:`/api/core`), then optionally bridge a bidirectional
:class:`MsgStream` between the two tasks.

.. d2:: diagrams/context_handshake.d2
   :caption: The ``open_context()`` <-> ``ctx.started()`` handshake.
   :margin:
   :alt: parent and child actor context handshake sequence

For the guided, example-driven tour see :doc:`/guide/context`; this
page is the precise API surface.

.. currentmodule:: tractor

The ``@context`` decorator
--------------------------

.. autofunction:: context

.. note::

   The decorated function **must** declare a parameter annotated
   ``tractor.Context`` (any param name works); the runtime injects
   the context instance there on each remote invocation. Pass
   ``pld_spec`` to type-restrict (and validate) the payloads this
   endpoint may shuttle — violations raise
   :class:`MsgTypeError`. See ``examples/typed_payloads.py``.

``Context``
-----------

.. autoclass:: Context
   :members: started,
             wait_for_result,
             cancel,
             cid,
             chan,
             side,
             cancel_called,
             cancelled_caught,
             cancel_acked,
             canceller,
             maybe_error,
             outcome

.. deprecated:: 0.1.0a6

   ``Context.result()`` warns; use :meth:`Context.wait_for_result`.

.. note::

   A :class:`Context` is **not** a :class:`trio.CancelScope`:
   :meth:`Context.cancel` requests cancellation of the *remote*
   peer task and does not cancel the local scope. If *you*
   requested the cancel, the resulting :class:`ContextCancelled`
   is absorbed at ``open_context()`` exit; a cancel originating
   anywhere else (the peer, or a third-party actor recorded in
   :attr:`ContextCancelled.canceller`) *is* raised locally. This
   self-vs-cross-cancel rule is the key to writing correct
   inter-actor teardown logic — see :doc:`/guide/context`.

Bidirectional streaming
-----------------------

.. autofunction:: tractor._streaming.open_stream_from_ctx

.. note::

   :func:`~tractor._streaming.open_stream_from_ctx` is bound as
   the **method-alias** ``Context.open_stream()`` — call it as
   ``async with ctx.open_stream() as stream:``. Both sides of the
   context must enter it for the dialog to be open.

.. autoclass:: MsgStream
   :members: send,
             receive,
             receive_nowait,
             aclose,
             subscribe,
             ctx,
             closed

.. note::

   A :class:`MsgStream` is one-shot use: once closed it can never
   be "re-opened" — open a fresh :class:`Context` instead. Remote
   end-of-stream surfaces as :class:`StopAsyncIteration` from
   ``async for``; un-consumed sends overrun the receiver and raise
   :class:`tractor._exceptions.StreamOverrun` unless the context
   was opened with ``allow_overruns=True``.

:meth:`MsgStream.subscribe` fans a single IPC stream out to
multiple *local* tasks via a
:class:`tractor.trionics.BroadcastReceiver` (see
:doc:`/api/trionics`); the underlying allocation is idempotent and
non-reversible for the stream's lifetime. See
``examples/streaming_broadcast_fanout.py`` for the pattern in
action.

Legacy one-way streaming
------------------------

.. autofunction:: stream

.. warning::

   ``@tractor.stream`` and ``Portal.open_stream_from()`` are the
   *legacy* one-way streaming API kept for backward compat: a
   plain async-generator function streamed parent-ward with no
   child-side receive leg. New code should use
   ``@tractor.context`` + ``ctx.open_stream()`` (bidirectional,
   SC-linked, typed). Note ``ctx`` is now a reserved param name
   for ``@context`` endpoints — ``@stream`` functions must use
   ``stream`` instead, and ``ctx.send_yield()`` is deprecated in
   favor of :meth:`MsgStream.send`.

.. seealso::

   :doc:`/api/errors` for :class:`ContextCancelled` /
   :class:`MsgTypeError` semantics, :doc:`/api/msg` for payload
   typing via ``pld_spec`` and codecs, :doc:`/api/trionics` for
   the broadcast fan-out machinery, and the guided tours in
   :doc:`/guide/streaming` + :doc:`/guide/cancellation`.

.. _exactly like trio: https://trio.readthedocs.io/en/latest/reference-core.html#cancellation-semantics
.. _nursery: https://trio.readthedocs.io/en/latest/reference-core.html#nurseries-and-spawning
