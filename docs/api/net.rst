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
