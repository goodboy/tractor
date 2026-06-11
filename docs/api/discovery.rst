Discovery and the registrar
===========================

Every actor registers its ``(name, uuid)`` and transport addresses
with a *registrar* actor — by default the root of the tree, or
whichever actor serves at the ``registry_addrs`` you boot with.
The discovery API lets any actor look up any other **by name** and
get back a connected :class:`~tractor.Portal`, giving you
service-discovery patterns (daemons, service trees, multi-host
meshes) without hard-coding addresses.

Lookups first scan already-connected peers before RPC-ing the
registrar, and multihomed results are ranked UDS > local TCP >
remote TCP. See ``examples/service_daemon_discovery.py`` for the
canonical daemon + lookup pattern.

.. currentmodule:: tractor

Lookup APIs
-----------

.. autofunction:: find_actor

.. autofunction:: wait_for_actor

.. autofunction:: query_actor

.. autofunction:: get_registry

.. note::

   :func:`find_actor` yields ``None`` when nothing is registered
   under the name (or raises with ``raise_on_none=True``);
   :func:`wait_for_actor` blocks until the name appears;
   :func:`query_actor` only *looks up* the address without
   connecting to the target.

The ``Registrar``
-----------------

.. autoclass:: Registrar
   :show-inheritance:

A :class:`Registrar` is just an :class:`~tractor.Actor` subtype
maintaining the name -> addresses table; you rarely touch it
directly beyond passing ``registry_addrs`` /
``ensure_registry=True`` to :func:`~tractor.open_root_actor`.
Check :attr:`Actor.is_registrar <tractor.Actor.is_registrar>` to
ask "am I it?".

Legacy ``Arbiter`` alias
------------------------

.. deprecated:: 0.1.0a6

   ``tractor.Arbiter`` survives only as a class alias of
   :class:`Registrar` and all "arbiter" terminology is replaced
   by "registrar"/"registry" across the API: ``get_arbiter()`` is
   removed (use :func:`get_registry`) and the ``arbiter_addr``
   kwarg is replaced by ``registry_addrs``.

.. seealso::

   :doc:`/api/core` for booting a registrar via
   ``open_root_actor(registry_addrs=...)``, :doc:`/api/ipc` for
   the transport/address model the registry stores, and
   :doc:`/guide/discovery` for the worked walkthrough.
