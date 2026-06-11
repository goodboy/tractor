IPC and logging
===============

Under every portal, context and stream sits a per-peer
:class:`~tractor.Channel`: a msgpack-typed messaging link wrapping
one OS transport connection. Transports are pluggable per actor
via ``enable_transports=['tcp' | 'uds']`` — TCP is the default,
UDS (unix domain sockets) gives you port-less, same-host IPC with
kernel-provided peer credentials for free — and exactly **one**
transport may currently be enabled per actor.

.. d2:: diagrams/runtime_stack.d2
   :caption: Where ``Channel`` sits in the runtime stack.
   :margin:
   :alt: layered runtime stack from app code down to transports

Addresses are "unwrapped" tuples at the API edges:
``('host', port)`` for TCP, filesystem-path pairs for UDS. For
the full layering story — transport protocols, the IPC server,
address types and the msg loop — see
:doc:`/explain/architecture`.

.. currentmodule:: tractor

``Channel``
-----------

.. autoclass:: Channel
   :members: from_addr,
             send,
             recv,
             aclose,
             connected,
             apply_codec,
             aid,
             laddr,
             raddr,
             closed

.. deprecated:: 0.1.0a6

   ``Channel.uid`` warns; use :attr:`Channel.aid` which carries
   richer (optional) identity fields beyond the legacy
   ``(name, uuid)`` pair.

.. note::

   You rarely construct a :class:`Channel` yourself — the runtime
   hands them out via :attr:`Portal.chan <tractor.Portal.chan>`
   and :attr:`Context.chan <tractor.Context.chan>`. Treat the
   send/recv surface as advanced API: normal apps should speak
   :class:`~tractor.MsgStream` instead.

Choosing a transport
--------------------

.. literalinclude:: ../../examples/uds_transport_actor_tree.py
   :caption: examples/uds_transport_actor_tree.py
   :language: python

Logging
-------

``tractor.log`` provides the structured, colorized console
logging used across the runtime — with actor-name + task-aware
record headers and extra log levels below :data:`logging.DEBUG`
(``'transport'``, ``'runtime'``, ``'cancel'``, ``'devx'``) for
spelunking the runtime itself. Use it for your app too: it's
already distributed-system aware.

.. currentmodule:: tractor.log

.. autofunction:: get_logger

.. autofunction:: get_console_log

.. note::

   The ``TRACTOR_LOGLEVEL`` env var overrides any caller-passed
   ``loglevel`` (e.g. to ``open_root_actor()``) so you can crank
   console verbosity without touching code; subactors inherit
   the root's level by default.

.. seealso::

   :doc:`/explain/architecture` for the transport/server
   internals, :doc:`/api/discovery` for how channel addresses
   get registered and found, and :doc:`/api/msg` for the codec
   layer every channel speaks.
