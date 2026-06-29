The ``Context``: a cross-actor task pair
=========================================

If you've written any trio_ you already know the contract: every
task lives in a nursery, errors always propagate, cancellation is
scoped, and nothing leaks. ``tractor`` extends that exact contract
*across processes* — the same guarantees from the seminal
`blog post`_, just with the nursery split across two memory
domains. The primitive that does it is :class:`tractor.Context`: a
**linked pair of tasks**, one in each of two actors, supervised as
a single `structured concurrency`_ (SC) scope over IPC.

.. d2:: diagrams/context_handshake.d2
   :caption: The SC-transitive supervision protocol, msg by msg.
   :alt: sequence diagram of the context handshake msg flow

Pretty much everything else is (or is slated to be) built on this
one primitive: ``ActorNursery.run_in_actor()`` is a convenience
for "spawn, open a context, await the result, tear down"; plain
``Portal.run()`` RPC is planned to be re-implemented on top of it;
the multi-process debugger's tree-wide REPL lock rides one. Grok
this page and the rest of the library reads as convenience
wrappers B)

The endpoint contract
---------------------

A context endpoint is an async function decorated with
:func:`tractor.context` which declares **a param annotated**
``tractor.Context`` — any param name you like, the annotation is
what's required:

.. code:: python

    @tractor.context
    async def trainer(
        ctx: tractor.Context,
        model: str,
    ) -> str:
        await ctx.started('ready')
        return f'trained {model}'

.. margin:: Who am I talking to?

   Inside any context task
   :func:`tractor.current_ipc_ctx` returns the
   ``Context`` bound to the current task; handy
   in helpers that don't take ``ctx`` explicitly.

The parent (aka "opener") side invokes it through a
:class:`tractor.Portal` using ``Portal.open_context()``, passing
any extra kwargs which are shipped over the wire as the remote
task's arguments. Since the target fn is referenced by module
path, that module must be listed in the peer actor's
``enable_modules`` allowlist — RPC capability is always opt-in.

The decorator also accepts a ``pld_spec``: a type (union) which
every payload in the dialog is validated against, upgrading your
msgs to a typed contract enforced via :exc:`tractor.MsgTypeError`.
Validation strictness follows the "`cheap or nasty`_"
`(un)protocol`_ pattern: the one-shot ``Started`` payload gets the
nasty treatment (stringently round-trip checked before it's even
sent) while high-rate stream payloads stay cheap (checked only
receiver side).

The handshake, on the wire
--------------------------

Every context runs one instance of ``tractor``'s "SC-transitive
supervision protocol": a tiny fixed grammar of msgspec_-typed msgs
encapsulating *all* RPC dialogs between actors. *Transitive*
because each IPC link obeys the same rules a local nursery does —
starts are acked, completion is awaited, errors and cancels always
relay — so chaining links across a process tree composes into one
tree-wide SC scope.

The figure up top shows a full dialog; in order:

``Start``
    sent by ``Portal.open_context()``: "schedule a task running
    this function with these kwargs".

``StartAck``
    the peer runtime confirms the task is scheduled and that the
    endpoint really is a context-style fn.

``Started``
    emitted when the child task calls
    :meth:`tractor.Context.started`; carries the first payload
    and unblocks the parent's entry of ``open_context()``.

``Yield``
    one per :meth:`tractor.MsgStream.send`, flowing in *either*
    direction while a stream is open.

``Stop``
    graceful end-of-stream: the far side's ``async for``
    terminates cleanly.

``Return``
    the child fn returned; its value becomes the context's final
    result. If the child raised instead, an ``Error`` msg takes
    this slot carrying the boxed traceback.

``ctx.started()``: just like ``task_status.started()``
*******************************************************

The startup phase is a deliberate clone of
:meth:`trio.Nursery.start` semantics: the child decides when it's
"up", optionally handing back a first value, and the parent stays
blocked until that moment:

.. code:: python

    # trio, in-process
    first = await nursery.start(child_fn)

    # tractor, cross-process
    async with portal.open_context(child_fn) as (ctx, first):
        ...

The ``as (ctx, first)`` tuple is exactly that pair: the
:class:`tractor.Context` handle plus whatever value the child
passed to ``await ctx.started(value)``. And readiness is not
optional — for instance opening a stream before ``.started()``
has been called raises a ``RuntimeError``; handshake first, then
dialog.

Bidirectional streaming over a context
--------------------------------------

The canonical ping-pong (design history: `#53`_, `#223`_) — a
full-duplex msg stream between a parent and its spawned peer:

.. literalinclude:: ../../examples/rpc_bidir_streaming.py
   :caption: examples/rpc_bidir_streaming.py
   :language: python

What's going on?

- ``start_actor()`` spawns the daemon-style subactor
  ``'rpc_server'`` with this very module in its allowlist.

- ``portal.open_context(simple_rpc, data=10)`` fires the
  ``Start`` msg then blocks until the child task calls
  ``await ctx.started(data + 1)`` — hence ``sent == 11``.

- both tasks enter ``ctx.open_stream()``: a stream dialog is only
  fully open once *each* side has entered its block.

- the parent seeds the first ``'ping'``; each side then echoes
  the other, one ``Yield`` msg per ``stream.send()``.

- after the 9th pong the parent ``break``\ s (10 pings sent in
  total) and exits its stream block, which sends ``Stop``; the
  child's ``async for`` completes gracefully and its ``else``
  clause asserts all 10 pings arrived.

- the 10th in-transit pong? Discarded by the implicit drain at
  ``open_context()`` exit, which runs the dialog down to the
  child's ``Return`` (here ``None``).

- daemon actors live until told otherwise:
  ``portal.cancel_actor()`` reaps the subactor explicitly.

Results: the ``Return`` leg
---------------------------

Every context resolves to a final outcome. Wait on it explicitly
from the parent side:

.. code:: python

    async with portal.open_context(ep) as (ctx, first):
        ...
        result = await ctx.wait_for_result()

or just exit the block — ``__aexit__`` implicitly drains the msg
flow until the ``Return`` (or ``Error``) arrives, discarding any
in-transit ``Yield``\ s on the way. Either way the rule of
`causality`_ holds exactly as in a local nursery: **the opener
never unblocks before the remote task is done**.

For post-hoc inspection (think supervision/restart logic) the ctx
also exposes ``Context.outcome``, ``.maybe_error`` and
``.has_outcome`` — where a "result" might well be the error the
dialog ended with.

Cancellation semantics
----------------------

The part you actually came for; read it twice B)

A context's two tasks are **cancel-scope-linked across the IPC
boundary**: whatever ends one side — error, cancellation, plain
old return — is relayed such that the other side ends
equivalently. No silent half-open dialogs, no orphaned remote
tasks, ever.

``ctx.cancel()`` cancels the *remote* task
*******************************************

:meth:`tractor.Context.cancel` requests cancellation of the
**remote** task only:

.. code:: python

    async with portal.open_context(ep) as (ctx, first):
        await accomplish_things(ctx)
        await ctx.cancel()  # remote task, NOT me

A :class:`tractor.Context` is **not** a :class:`trio.CancelScope`:
the call doesn't (and can't) cancel your local task. It sends the
cancel request and waits a bounded ``timeout`` for the peer
runtime's ``CancelAck``, then your code proceeds to the block exit
as normal.

Compare scopes here: ``Portal.cancel_actor()`` is the big hammer
which cancels the peer's **entire runtime** (and thus process);
``ctx.cancel()`` is the per-dialog scalpel.

``ContextCancelled`` and the absorption rule
*********************************************

When a context task gets cancelled *by request* the requestee's
runtime reports back with a :exc:`tractor.ContextCancelled`
("ctxc") whose ``.canceller`` field holds the uid of the actor
which asked. That one field decides what you observe:

**you requested it**
    i.e. ``ctxc.canceller == tractor.current_actor().uid``: the
    ctxc is **absorbed** at ``open_context()`` exit — nothing
    raises in your block. You asked for a graceful stop and got
    it; if you care, ``await ctx.wait_for_result()`` hands the
    ctxc back as a plain *value* for inspection.

**anyone else requested it**
    the peer cancelling itself, or some third actor cancelling it
    from the side: the ctxc **is raised** in your block. From
    your scope's perspective a task you depend on was killed out
    from under you and SC demands you hear about it — exactly
    like a sibling crash in a `nursery`_.

In code:

.. code:: python

    try:
        async with portal.open_context(ep) as (ctx, first):
            ...
    except tractor.ContextCancelled as ctxc:
        # can only be a peer- or third-party cancel;
        # self-requested cancels are absorbed at exit.
        assert ctxc.canceller != tractor.current_actor().uid

This self- vs cross-cancel split is what makes explicit teardown
*composable*: a supervisor cancels its dialogs without try/except
noise, while unexpected cancellation anywhere in the tree still
propagates loudly like any other failure.

.. warning::

   Once ``ctx.cancel()`` has been called the dialog is done: a
   subsequent ``ctx.open_stream()`` raises ``RuntimeError``.

For introspection the ctx exposes trio-flavored status props:
``.cancel_called`` (this side requested), ``.cancel_acked`` (peer
confirmed), ``.cancelled_caught`` and ``.canceller`` —
deliberately mirroring :class:`trio.CancelScope` naming.

Errors propagate, both ways
---------------------------

A crash on either end tears down the pair, SC style:

- **child raises**: the exception ships back as an ``Error`` msg
  and re-raises in the parent block boxed as a
  :exc:`tractor.RemoteActorError`; the original class rides along
  as ``.boxed_type`` with ``.src_uid`` naming the crashed actor.

- **parent raises** (or is cancelled) inside the block: an
  equivalent error/cancel is relayed to the child task so it can
  never outlive the dialog.

.. code:: python

    try:
        async with portal.open_context(ep) as (ctx, first):
            ...
    except tractor.RemoteActorError as rae:
        if rae.boxed_type is ValueError:
            ...  # remote ValueError, type preserved

Errors that hop through intermediary actors on their way up the
tree ("inceptions" XD) keep the full relay trail in
``.relay_uid`` / ``.relay_path``. Payloads violating your declared
``pld_spec`` surface as the IPC analog of a ``TypeError``:
:exc:`tractor.MsgTypeError`.

Overruns and backpressure
-------------------------

Stream msgs land in a bounded per-context buffer on the receiver
side. A sender that outpaces a non-consuming receiver *overruns*
it and the runtime raises ``StreamOverrun`` (from
``tractor._exceptions``; also a :exc:`trio.TooSlowError`) instead
of buffering without bound — SC discipline applies to memory too.

Your knobs:

- ``msg_buffer_size`` on ``ctx.open_stream()`` sizes the buffer.

- ``allow_overruns=True`` (on ``Portal.open_context()`` and/or
  ``ctx.open_stream()``) opts in to absorbing overflow instead of
  erroring — reasonable for bursty telemetry-ish feeds, just know
  you're trading the error for extra buffering.

One context, one stream
-----------------------

A ``MsgStream`` is strictly **one-shot use**: once it closes —
gracefully or not, from either side — it can never be re-opened
on the same ctx. Want another round with the same peer? Open a
fresh context; they're cheap. The full close-vs-cancel teardown
story lives in :doc:`/guide/streaming`.

.. rubric:: Where to next?

:doc:`/guide/streaming` covers the rest of the msg-moving story:
the legacy one-way API, multi-actor pipelines and in-actor
broadcast fan-out. For exhaustive API detail see
:class:`tractor.Context`, :class:`tractor.MsgStream` and
:exc:`tractor.ContextCancelled`.

.. _trio: https://github.com/python-trio/trio
.. _structured concurrency: https://en.wikipedia.org/wiki/Structured_concurrency
.. _blog post: https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/
.. _nursery: https://trio.readthedocs.io/en/latest/reference-core.html#nurseries-and-spawning
.. _causality: https://vorpus.org/blog/some-thoughts-on-asynchronous-api-design-in-a-post-asyncawait-world/#c-c-c-c-causality-breaker
.. _cheap or nasty: https://zguide.zeromq.org/docs/chapter7/#The-Cheap-or-Nasty-Pattern
.. _(un)protocol: https://zguide.zeromq.org/docs/chapter7/#Unprotocols
.. _msgspec: https://jcristharif.com/msgspec/
.. _#53: https://github.com/goodboy/tractor/issues/53
.. _#223: https://github.com/goodboy/tractor/issues/223
