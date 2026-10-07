# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

'''
Network declarations, bindspaces and tunnels.

Public symbols are imported and cached on first access so importing
this package does not load optional network dependencies.

'''
from importlib import import_module


_SYMBOL_MODULES: dict[str, str] = {
    'Bindspace': '._bindspace',
    'BindspaceKind': '._bindspace',
    'BindspaceLifecycle': '._bindspace',
    'BindspaceOwnership': '._bindspace',
    'BindspaceRef': '._bindspace',
    'BindspaceSpec': '._bindspace',
    'CURRENT_NETNS': '._bindspace',
    'attach_netns': '._bindspace',
    'open_bindspace': '._bindspace',
    'open_netns': '._bindspace',
    'TunnelledAddress': '._tunnel',
    'TunnelSpec': '._tunnel',
    'WGTunnelSpec': '._tunnel',
    'WGInterfaceConfig': '._tunnel',
    'WGPeerConfig': '._tunnel',
    'WGRole': '._tunnel',
    'mb_pubkey': '._tunnel',
    'mk_wg_maddr': '._tunnel',
    'open_wg_bindspace': '._tunnel',
    'open_wg_iface': '._tunnel',
    'parse_wg_maddr': '._tunnel',
    'read_wg_peers': '._tunnel',
    'read_wg_pubkey': '._tunnel',
    'strip_tunnels': '._tunnel',
    'tunnels_of': '._tunnel',
    'verify_wg_peer': '._tunnel',
    'wg8_pubkey': '._tunnel',
    'mk_maddr': '..discovery._multiaddr',
    'parse_maddr': '..discovery._multiaddr',
    'parse_endpoints': '..discovery._multiaddr',
}

__all__: tuple[str, ...] = tuple(_SYMBOL_MODULES)


def __dir__() -> list[str]:
    '''
    Advertise the complete lazy public API.

    '''
    return sorted(set(globals()) | set(__all__))


def __getattr__(name: str) -> object:
    '''
    Import and cache one public network symbol on first access.

    '''
    try:
        module_name: str = _SYMBOL_MODULES[name]
    except KeyError:
        raise AttributeError(
            f'module {__name__!r} has no attribute {name!r}'
        ) from None

    value: object = getattr(
        import_module(module_name, __name__),
        name,
    )
    globals()[name] = value
    return value
