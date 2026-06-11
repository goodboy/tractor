Cross-process streaming
=======================

Spawning processes is the boring half of ``tractor``: the **real
cool stuff** is the native support for cross-process *streaming*.
Yes, you saw it here first — 2-way msg streams with reliable,
transitive setup/teardown semantics, wired straight into the
runtime's `structured concurrency`_ (SC) supervision machinery so
that *how* a stream ends is part of the protocol.

No broker, no topic exchange, no IDL compiler. The IPC layer is a
deliberately "`cheap or nasty`_" `(un)protocol`_: a tiny set of
msgspec_-typed msgs over a transport (TCP or UDS today) with
payload typing opt-in per dialog — handshake msgs get the *nasty*
treatment (strict validation) while high-rate stream payloads
stay *cheap* (receiver-side checks only). See
:doc:`/guide/context` for the typed ``pld_spec`` contract bits.

Two ways to stream
------------------

.. margin:: It's a ``trio.abc.Channel``

   :class:`tractor.MsgStream` implements
   :class:`trio.abc.Channel` — ``send()``,
   ``receive()``, async-iteration, ``aclose()`` —
   so trio-generic channel code drives an IPC
   stream unchanged.

- **Bidirectional, context-based**: open a
  :class:`tractor.Context` to a peer task then enter
  ``ctx.open_stream()`` for a full-duplex
  :class:`tractor.MsgStream`. This is the modern core API, taught
  end-to-end in :doc:`/guide/context`; we won't re-teach it here.

- **One-way, portal-based**: point
  :meth:`tractor.Portal.open_stream_from` at a plain async
  generator fn in the peer actor. Legacy, but perfectly fine for
  simple produce/consume pipelines — and it powers the classic
  examples below.

Rule of thumb: if the consumer ever needs to *talk back* — acks,
control msgs, a final result — use a context. If it's a pure
pipeline stage, either works and the one-way form is less typing.

One-way streaming from an async generator
-----------------------------------------

The OG api. Write an async generator in the target actor's
module; iterate its yields from the spawning side:

.. literalinclude:: ../../examples/asynchronous_generators.py
   :caption: examples/asynchronous_generators.py
   :language: python

Each ``yield`` crosses the process boundary as one msg and feeds
the parent's ``async for``. When the consumer ``break``\ s out
and exits the ``open_stream_from()`` block the far-end generator
task is cancelled for you: the producer's lifetime is *coupled to
the consumer's scope* so a one-way stream can never leak a remote
task.

Any extra kwargs (``stream_data, seed=100`` style) are forwarded
to the remote generator's call, and a non-async-gen target is
rejected up front with a ``TypeError``.

.. note::

   No decorator required — any plain async-gen fn works. You may
   still meet ``@tractor.stream`` in the wild; it's the legacy
   marker for one-way endpoints and sticks around only for
   compat (heads up: the param name ``ctx`` is reserved for
   ``@context`` endpoints nowadays, so legacy fns should call
   theirs ``stream``). New code wanting anything fancier than a
   one-way pipe should use :func:`tractor.context` +
   ``ctx.open_stream()``.

.. warning::

   One-way means one way: there's no sending *to* the generator
   side and no graceful consumer-to-producer stop msg — the
   teardown above is cancel-based. Needing upstream control flow
   is the sign you've outgrown this API.

A full-fledged streaming service
--------------------------------

Now let's get fancy: compose one-way streams through a nested
actor tree and you've got yourself a fan-in pipeline.

.. d2:: diagrams/streaming_pipeline.d2
   :caption: Four actors, three streams, one deduped feed.
   :alt: two streamer actors fan in to an aggregator then root

.. literalinclude:: ../../examples/full_fledged_streaming_service.py
   :caption: examples/full_fledged_streaming_service.py
   :language: python

What's going on?

- the root actor spawns ``'aggregator'`` which opens its *own*
  actor nursery and spawns ``'streamer_1'`` + ``'streamer_2'``: 4
  processes total, supervision nested two levels deep with zero
  special casing.

- ``aggregate()`` opens a one-way stream from each streamer and
  fans both into a single :func:`trio.open_memory_channel` via
  one local trio task per portal — in-actor fan-in riding trio's
  built-in backpressure end-to-end.

- duplicates get dropped via a ``set`` and the deduped sequence
  is *re-yielded* upward: ``aggregate()`` is itself an async gen
  being consumed over IPC by the root. Streams compose.

- when the seed runs out the streamer gens finish, the memory
  channel drains closed, the aggregator's gen returns and the
  root's ``async for`` ends; ``await an.cancel()`` then reaps the
  subtree. Every exit is awaited — if you can produce a zombie
  process from this, it **is a bug**.

Watch the tree breathe while it runs, using the README's
signature process-monitor incantation::

    $TERM -e watch -n 0.1  "pstree -a $$" \
        & python examples/full_fledged_streaming_service.py \
        && kill $!

No extra threads, no fancy semaphores, no futures; all we need is
``tractor``'s IPC.

Two streams, one portal
-----------------------

Every ``open_stream_from()`` call starts its *own* remote task —
even through the same portal — so two local consumer tasks can
independently stream the same generator fn concurrently, both
dialogs multiplexed over the single underlying IPC channel:

.. literalinclude:: ../../examples/multiple_streams_one_portal.py
   :caption: examples/multiple_streams_one_portal.py
   :language: python

The add-else-remove trick on the shared ``consumed`` list is the
proof: each value arrives in *both* streams, getting appended by
whichever task sees it first and removed by the other, so the
list always ends up empty. Two streams, same data, zero
interference.

This works because every dialog is keyed by its own context id
(``Context.cid``) — any number of concurrent streams, contexts
and one-shot RPCs share a single underlying
:class:`tractor.Channel` per peer pair.

Fan-out inside an actor: ``MsgStream.subscribe()``
--------------------------------------------------

The inverse pattern: *one* IPC stream feeding *many* local tasks.
Instead of paying for N redundant cross-process streams, call
:meth:`tractor.MsgStream.subscribe` to get a
``BroadcastReceiver`` — a tokio-style broadcast channel from
``tractor.trionics`` — which copies every received value to each
subscribed task:

.. literalinclude:: ../../examples/streaming_broadcast_fanout.py
   :caption: examples/streaming_broadcast_fanout.py
   :language: python

Each task entering ``stream.subscribe()`` receives its own copy
of everything sent from that point on. The underlying stream
keeps pace with the *fastest* subscriber; a task falling more
than the buffered window behind has its next receive raise
``tractor.trionics.Lagged`` to say it lost data.

The broadcast handle stays duplex btw: it proxies ``send()``
through to the underlying stream, so each subscriber task can
keep talking upstream while consuming its fan-out copy.

.. warning::

   ``.subscribe()`` is **idempotent and non-reversible**: the
   first call permanently swaps the stream's receive machinery
   over to the internally allocated broadcaster. There's no
   un-subscribing back to the raw stream, so make sure you're ok
   with the (theoretical) overhead before opting in.

Consuming: ``async for`` and friends
------------------------------------

``async for msg in stream:`` is just sugar over repeated
``await stream.receive()``. The receive-side surface:

- ``receive()`` — next msg, or raises :exc:`trio.EndOfChannel`
  on a graceful far-end close (``async for`` translates that
  into a clean loop exit for you).

- ``receive_nowait()`` — opportunistic, non-blocking drain.

- ``closed`` — property flagging an already-ended stream.

Send-side it's just ``await stream.send(data)`` — one ``Yield``
msg per call carrying any msgspec_-encodable payload (or
whatever your ``pld_spec`` permits, see :doc:`/guide/context`).

End-of-stream: close vs. cancel
-------------------------------

How a stream ends is part of the protocol; the runtime keeps the
polite case and the violent case distinct:

- **graceful close**: the far side exits its stream block, its
  async gen returns, or it calls ``await stream.aclose()``. A
  ``Stop`` msg is sent so your ``async for`` simply ends
  (``StopAsyncIteration``, via :exc:`trio.EndOfChannel` under the
  hood). A normal, non-error ending — the dialog's result phase
  proceeds as usual.

- **cancel or error**: no ``Stop`` is sent. Instead the
  cancel/error itself is relayed so the far end *knows* the
  dialog did not end on purpose and raises accordingly — a
  :exc:`tractor.ContextCancelled`, a boxed
  :exc:`tractor.RemoteActorError`, etc. See the cancellation
  section of :doc:`/guide/context` for exactly who raises what.

Tying it together: every ``MsgStream`` is **one-shot use**. Both
endings are final — once closed a stream can't be re-opened and
the supported "retry" is opening a fresh :class:`tractor.Context`
(they're cheap).

.. seealso::

   - :doc:`/guide/context` — the full ``Context`` lifecycle: the
     handshake, results, cancellation semantics and the
     overrun/backpressure knobs.

   - :class:`tractor.MsgStream` and
     :meth:`tractor.Portal.open_stream_from` API docs.

   - The zguide chapters our wire philosophy is named after:
     "`cheap or nasty`_" and `(un)protocol`_\ s.

.. _structured concurrency: https://en.wikipedia.org/wiki/Structured_concurrency
.. _cheap or nasty: https://zguide.zeromq.org/docs/chapter7/#The-Cheap-or-Nasty-Pattern
.. _(un)protocol: https://zguide.zeromq.org/docs/chapter7/#Unprotocols
.. _msgspec: https://jcristharif.com/msgspec/
