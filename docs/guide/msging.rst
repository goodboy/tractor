Typed messaging
===============
Every value that crosses an actor boundary rides inside a typed
msg. ``tractor`` ships a small, fixed family of msg types, the
"SC-transitive supervision protocol", which encapsulates *all*
RPC dialogs in the tree such that `structured concurrency`_ (SC)
semantics -- parent-child task linkage, error propagation,
graceful cancellation -- hold across every process hop. On top of
that
protocol you can layer **your own** payload type contracts,
per-endpoint, and have them enforced at runtime by the codec.

.. note::

   Older posts and readmes claim ``tractor`` "uses
   ``msgpack``(-python)" on the wire. The wire *encoding* is still
   msgpack, but since ``0.1.0a5`` all codec work is done by
   msgspec_ against a strictly-typed, tagged-union msg-spec;
   neither ``msgpack-python`` nor ``u-msgpack`` are involved.

The wire format
---------------
Each protocol msg is a :class:`msgspec.Struct` subtype declared
with ``tag=True, tag_field='msg_type'``, so the full set decodes
as a `tagged union`__ with zero dispatch code of our own. The
payload-carrying msgs all inherit from ``PayloadMsg`` which boxes:

__ https://jcristharif.com/msgspec/structs.html#tagged-unions

- ``.cid`` -- the "context id" identifying which dialog (i.e.
  which ``Context``) the msg belongs to,
- ``.pld`` -- the *payload*, aka your app's actual data.

Decoding is deliberately two-layered:

- the **transport codec** decodes only the protocol *envelope*,
  intentionally leaving ``.pld`` as raw bytes
  (:class:`msgspec.Raw`),
- a **per-context payload-receiver** (the internal ``PldRx``) then
  decodes each ``.pld`` against *that* dialog's user-defined type
  spec.

This split is what lets every ``Context`` carry its own msg-spec
without reconfiguring the shared transport, keeps the runtime's
own traffic immune to your app's spec choices, and makes any
validation failure attributable to exactly one dialog (and thus
one task pair) instead of nuking the whole channel.

The protocol family
-------------------
The entire msg-spec is ten types, all importable from
``tractor.msg`` (defined in ``tractor.msg.types``):

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - msg type
     - role
   * - ``Aid``
     - actor-identity handshake; the first thing two peers
       exchange on connect (name, uuid, pid).
   * - ``SpawnSpec``
     - parent -> child runtime config sent right after ``Aid``:
       enabled modules, registry/bind addrs, runtime vars.
   * - ``Start``
     - request to remotely schedule an RPC task: target
       namespace + func name, kwargs and the caller's uid.
   * - ``StartAck``
     - the callee's ack declaring the endpoint's "functype":
       ``asyncfunc``, ``asyncgen`` or ``context``.
   * - ``Started``
     - the first value passed to ``ctx.started()``; completes
       the context handshake.
   * - ``Yield``
     - one streamed value per ``MsgStream.send()`` call.
   * - ``Stop``
     - graceful stream termination; the IPC rendition of
       ``StopAsyncIteration``.
   * - ``Return``
     - the final return value of the remote task fn.
   * - ``CancelAck``
     - ``bool`` result of a runtime cancel-request; always
       decodable so graceful cancellation can never be broken
       by a custom msg-spec.
   * - ``Error``
     - a boxed remote exception (src uid, relay path, tb str,
       ..) relayed for local re-raise as
       :class:`tractor.RemoteActorError`.

Squint and you'll see an SC task scope serialized onto the wire:
every dialog opens with ``Start``/``StartAck`` (plus ``Started``
for ``@tractor.context`` endpoints), optionally streams
``Yield``-s until a ``Stop``, and **always** terminates with
exactly one of ``Return``, ``Error`` or ``CancelAck``. That 1:1
mapping of msg sequence onto a cross-process task pair is why we
call the protocol *SC-transitive*: supervision semantics survive
every hop of the tree. In `(un)protocol`_ terms it's our "SC
dialog un-protocol".

For introspection the union alias ``tractor.msg.MsgType``, the
list ``__msg_types__`` and the spec alias ``__msg_spec__`` are
all exported.

Payload typing with ``pld_spec``
--------------------------------
By default ``.pld`` may be any msgspec-supported type, i.e. the
spec is ``Any``. To constrain a single endpoint's dialog, pass
a type (union) to the decorator:
``@tractor.context(pld_spec=MyStruct|None)``. The spec then
applies to all payload-carrying msgs of that dialog --
``Started``, ``Yield`` and ``Return`` -- on both sides of the
IPC. Pro tip: keep ``None`` in your union since most endpoints
implicitly ``return None`` and a bare ``ctx.started()`` ships
``None`` too.

.. literalinclude:: ../../examples/typed_payloads.py
   :caption: examples/typed_payloads.py
   :language: python

What's going on?

- the payload schema is just a :class:`msgspec.Struct` subtype;
  anything msgspec can tag and decode works, including unions
  of structs, builtins and containers.
- decorating with ``@tractor.context(pld_spec=...)`` attaches the
  spec to the endpoint; both peers' payload-receivers now decode
  this dialog's payloads against it. No spec sharing files, no
  IDL compiler, the contract *is* the Python type.
- the happy path looks identical to untyped code: the child calls
  ``await ctx.started(<conforming value>)``, streams or returns
  more conforming values, and the parent receives fully decoded
  struct instances (not dicts!) on its side.
- the sad path is the point: shipping a value *outside* the spec
  raises :class:`tractor.MsgTypeError`, which the example catches
  to show off the failure mode; see the anatomy section below for
  exactly where it gets raised.

Where validation happens: cheap-or-nasty
----------------------------------------
A naive impl would validate every payload on both send *and*
receive, doubling your codec bill exactly where throughput
matters most. Instead ``tractor`` follows the 0mq lords'
"`cheap or nasty`_" pattern: be **nasty** (strict, eager,
expensive) on the rare control msgs and **cheap** (lazy, fast) on
the high-rate stream path.

- ``Started`` is the *only* payload that gets the full nasty
  treatment: ``ctx.started(value)`` stringently
  **roundtrip-checks** the encoded msg against the dialog's spec
  *before* sending, so a non-conforming first value raises
  :class:`tractor.MsgTypeError` immediately in the child and
  never even hits the wire. (You can opt out per-call with
  ``ctx.started(..., validate_pld_spec=False)`` if you measure
  a real cost.)
- ``Yield`` payloads are **never** checked inside
  ``MsgStream.send()``; they're validated receiver-side on each
  ``MsgStream.receive()``. A violation raises a ``MsgTypeError``
  in the receiver *and* relays an ``Error`` msg back so the
  offending sender gets one raised too.
- the remaining control msgs (``Start``, ``Return``) are likewise
  validated such that violations raise in the **sending** actor,
  pointing the traceback at the code that actually goofed.

Anatomy of a ``MsgTypeError``
-----------------------------
:class:`tractor.MsgTypeError` is the IPC equivalent of a builtin
``TypeError``: a ``RemoteActorError`` subtype raised whenever
a msg fails to decode against the active spec. The useful bits:

- ``.bad_msg`` -- the offending msg instance (reconstructed from
  its wire form when necessary) so you can inspect the actual
  ``.pld`` that broke the contract.
- ``.expected_msg_type`` -- the protocol msg type the bad msg was
  (supposed to be) decoded as, e.g. ``Started[Point]``.
- plus the standard ``RemoteActorError`` goodies: ``.boxed_type``,
  ``.src_uid``, ``.ipc_msg`` and the fancy ``.pformat()`` tb-box
  rendering.

Practical reading guide: a *sender-side* MTE (``Started``,
``Return``) points straight at your offending ``await
ctx.started()`` or ``return`` statement, while a *receiver-side*
MTE (``Yield``) surfaces from the consumer's ``receive()`` call
with the relay copy delivered back to the producer. Either way
the failure is scoped to that one dialog; sibling contexts on the
same channel keep right on trucking.

Custom wire types: ``mk_codec()`` and friends
---------------------------------------------
msgspec covers a wide set of `builtin types`__ natively; for
anything else you teach the codec via extension hooks. The
easiest path is per-endpoint: ``@tractor.context()`` accepts
``enc_hook``/``dec_hook`` params right alongside ``pld_spec``.
For full control build and apply a codec yourself; encode-side:

__ https://jcristharif.com/msgspec/supported-types.html

.. code:: python

    from tractor.msg import mk_codec, apply_codec

    codec = mk_codec(
        enc_hook=nsp_to_str,        # your-type -> wire-type
        ext_types=[NamespacePath],
    )
    with apply_codec(codec):        # ContextVar-scoped override
        ...  # msgs sent by this task now encode NSPs

and decode-side, scoped to an open context (note the import from
``tractor.msg._ops``, not yet re-exported):

.. code:: python

    from tractor.msg._ops import limit_plds

    with limit_plds(
        NamespacePath,
        dec_hook=str_to_nsp,        # wire-type -> your-type
        ext_types=[NamespacePath],
    ):
        ...  # this dialog's payloads decode as NSPs

``apply_codec()`` is ``ContextVar``-scoped: it overrides the
codec for the current task (and only that task), not the whole
process. For complete working flows, including hook pairing rules
and roundtrip cases, see ``tests/msg/test_ext_types_msgspec.py``
and ``tests/msg/test_pldrx_limiting.py``.

The runtime dogfoods this pattern with
:class:`tractor.msg.NamespacePath`: a ``str``-subtype shaped like
``'module.path:obj_name'`` used for every RPC target reference.
It ships over the wire as a plain string yet ``.load_ref()``-s
back to the actual object in the receiving actor's memory domain;
a minimal "pointer type" for shared-nothing systems.

Toward capability-based msging
------------------------------
The ``pld_spec`` + codec-hook layer is the foundation for the
long-game: **capability-based msging** where each dialog's
type contract doubles as a capability grant, negotiated as part
of the protocol itself. The epic is tracked in `#196`_ (evolving
the original typed-proto work in `#36`_), and the most recent
concrete step is `#365`_ — driving the whole ``pld_spec`` off
plain type-annotations (e.g. annotating a context's
``open_stream()`` with ``msgspec.Struct`` subtypes) instead of
explicit ``pld_spec=`` kwargs.

You don't have to wait for that, though: the decorator-level
``@tractor.context(pld_spec=...)`` shown above is already the
*higher-level* way to pin a dialog's payload contract, while
``tractor.msg._ops.limit_plds()`` is the lower-level, per-block
escape hatch. Both are exercised end-to-end in
``tests/msg/test_pldrx_limiting.py`` and
``tests/msg/test_ext_types_msgspec.py``.

On the codec-hook side, the ``enc_hook``/``dec_hook`` pair is
today only reachable via ``tractor.msg._ops``; a public *factory*
API for them is drafted in `#376`_ (from
`@guilledk <https://github.com/guilledk>`_, on the
`auto_codecs <https://github.com/goodboy/tractor/tree/auto_codecs>`_
branch) — the likely long-term home for custom-type
(de)serialization.

If strongly-typed distributed systems get you going, we'd love
your input on any of the above.

Where to next?
--------------
.. seealso::

   - :doc:`/guide/context` for the dialog API these msgs
     implement: ``started()``, streams and results.
   - :doc:`/guide/asyncio` for shuttling (typed) payloads into
     ``asyncio``-land via an infected subactor.
   - the msgspec_ docs for everything your payload types can be.

.. _structured concurrency: https://en.wikipedia.org/wiki/Structured_concurrency
.. _msgspec: https://jcristharif.com/msgspec/
.. _cheap or nasty: https://zguide.zeromq.org/docs/chapter7/#The-Cheap-or-Nasty-Pattern
.. _(un)protocol: https://zguide.zeromq.org/docs/chapter7/#Unprotocols
.. _#196: https://github.com/goodboy/tractor/issues/196
.. _#36: https://github.com/goodboy/tractor/issues/36
.. _#365: https://github.com/goodboy/tractor/issues/365
.. _#376: https://github.com/goodboy/tractor/pull/376
