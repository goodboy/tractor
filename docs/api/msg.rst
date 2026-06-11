Typed messaging: ``tractor.msg``
================================

All inter-actor communication rides a small, strictly-typed
msgpack wire protocol built from :class:`msgspec.Struct` types —
the "SC-shuttle" protocol that powers contexts, streams, RPC and
cancellation. You normally never touch these msg types directly
(the :class:`~tractor.Context` API speaks them for you) but you
*do* use this subpackage to define **payload type contracts**: per
endpoint via ``@tractor.context(pld_spec=...)`` or per channel via
custom codecs.

Violations of an active msg-spec surface as
:class:`~tractor.MsgTypeError` (see :doc:`/api/errors`); the full
typed-payload workflow is shown in ``examples/typed_payloads.py``.

.. currentmodule:: tractor.msg

The protocol message set
------------------------

.. autosummary::
   :nosignatures:

   PayloadMsg
   Aid
   SpawnSpec
   Start
   StartAck
   Started
   Yield
   Stop
   Return
   CancelAck
   Error

``Aid`` (identity handshake) and ``SpawnSpec`` (parent -> child
init) run at connection setup; ``Start``/``StartAck`` initiate an
RPC task; ``Started``/``Yield``/``Stop``/``Return`` are the
:class:`~tractor.Context` dialog phases; ``CancelAck`` and
``Error`` close the loop on cancellation and (boxed) failure.
``Msg`` is a legacy alias of ``PayloadMsg``. The union of all of
the above is exported as ``MsgType`` (also ``__msg_spec__``).

.. automodule:: tractor.msg.types
   :no-members:

.. currentmodule:: tractor.msg

Codec construction and override
-------------------------------

.. autofunction:: mk_codec

.. autoclass:: MsgCodec
   :members: encode,
             decode,
             msg_spec

.. autofunction:: mk_dec

.. autoclass:: MsgDec
   :members: decode,
             spec

.. autofunction:: apply_codec

.. autofunction:: current_codec

.. note::

   :func:`apply_codec` swaps the codec via a
   :class:`contextvars.ContextVar` — the override only applies to
   the *current task* (and tasks it starts), not sibling tasks
   already running in the actor. Payload-decoding is layered: the
   outer codec leaves ``.pld`` fields as ``msgspec.Raw`` and each
   context's payload-receiver decodes them against *its* spec
   (the "cheap-or-nasty" validation pattern).

Namespace pointers
------------------

.. autoclass:: NamespacePath
   :members: from_ref,
             load_ref,
             to_tuple

The ``'module.path:obj_name'`` :class:`str`-subtype used to
address every RPC target function over the wire (same format as
:func:`pkgutil.resolve_name`).

Pretty structs
--------------

.. autoclass:: Struct
   :show-inheritance:

A :class:`msgspec.Struct` subtype with a multi-line pretty
``__repr__`` — handy as a base for your own IPC payload types so
crash logs stay readable.

.. seealso::

   :doc:`/api/context` for where ``pld_spec`` typing plugs into
   the ``@context`` decorator, :doc:`/api/errors` for
   :class:`~tractor.MsgTypeError` semantics, and
   :doc:`/guide/msging` for the guided typed-messaging tour.
