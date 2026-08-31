Network declarations and lifecycles
===================================

``tractor.net`` provides address composition, bindspace declarations,
and tunnel configuration. The package is lazy: importing
``tractor`` or ``tractor.net`` does not load multiaddr, WireGuard, or
pyroute2 implementation modules until a public symbol is used.

Multiaddr helpers
-----------------

.. currentmodule:: tractor.net

.. autofunction:: mk_maddr

.. autofunction:: parse_maddr

.. autofunction:: parse_endpoints

Bindspaces
----------

.. autoclass:: BindspaceSpec

.. autoclass:: BindspaceRef

.. autoclass:: Bindspace

.. autofunction:: attach_netns

.. autofunction:: open_netns

.. autofunction:: open_bindspace

Root actor composition
----------------------

A live :class:`Bindspace` can scope the root actor itself. Compose the
bindspace manager outside :func:`tractor.open_root_actor` so its network
namespace remains pinned for the complete actor runtime::

    async with tractor.net.open_wg_bindspace(
        bindspace_spec=bindspace_spec,
        layers=layers,
        role='listen',
    ) as bindspace:
        async with tractor.open_root_actor(
            bindspace=bindspace,
            enable_transports=['uds'],
        ) as root_actor:
            ...

Root entry happens before registry probes, IPC listeners, runtime sockets,
or actor startup. On every exit, including cancellation or a body error,
the calling thread is restored to its original network namespace before
``open_root_actor()`` returns. The root context duplicates the live
``Bindspace.namespace_fd`` and never consumes or closes the descriptor
owned by ``open_wg_bindspace()``. Default child processes inherit the root
namespace naturally; passing an explicit alternate child ``bindspace``
continues to use that spawn backend's existing behavior. Bound roots reject
the persistent ``mp_forkserver`` backend because a server started by an
earlier runtime may retain that runtime's network namespace.

This is the current low-level composition API. A future convenience API
may accept a tunnel-bearing multiaddr, realize its WireGuard bindspace
internally, and supply that live capability to root startup.

Tunnels and WireGuard
---------------------

.. autoclass:: TunnelledAddress

.. autoclass:: WGTunnelSpec

.. autoclass:: WGInterfaceConfig

.. autoclass:: WGPeerConfig

.. autofunction:: parse_wg_maddr

.. autofunction:: mk_wg_maddr

.. autofunction:: strip_tunnels

.. autofunction:: tunnels_of

.. autofunction:: open_wg_iface

.. autofunction:: open_wg_bindspace

.. autofunction:: read_wg_pubkey

.. autofunction:: read_wg_peers

.. autofunction:: verify_wg_peer
