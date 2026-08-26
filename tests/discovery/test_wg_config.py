'''
Process-local WireGuard interface configuration contracts.

'''
from __future__ import annotations

import msgspec
import pytest

from tractor.discovery import WGInterfaceConfig
from tractor.msg import ProcessLocal


_PRIVATE_KEY: str = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='
_PRESHARED_KEY: str = 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBA='


def test_wg_config_is_process_local_and_redacted() -> None:
    '''
    Private WireGuard configuration must neither print nor cross IPC.

    Construct a complete local config and prove its public routing
    policy remains inspectable while both keys are absent from repr.
    Verify `ProcessLocal` blocks default msgpack encoding.

    '''
    config: WGInterfaceConfig = WGInterfaceConfig(
        private_key=_PRIVATE_KEY,
        addresses=('10.0.0.1/24', 'fd00::1/64'),
        allowed_ips=('10.1.0.0/16', 'fd01::/64'),
        listen_port=51820,
        preshared_key=_PRESHARED_KEY,
        persistent_keepalive=25,
    )

    config_repr: str = repr(config)
    assert isinstance(config, ProcessLocal)
    assert _PRIVATE_KEY not in config_repr
    assert _PRESHARED_KEY not in config_repr
    assert config.addresses[0] in config_repr
    assert config.allowed_ips[0] in config_repr
    with pytest.raises(
        TypeError,
        match='_ProcessLocalToken.*unsupported',
    ):
        msgspec.msgpack.encode(config)


@pytest.mark.parametrize(
    ('kwargs', 'error'),
    (
        pytest.param(
            {'private_key': 'not-base64'},
            ValueError,
            id='private-key',
        ),
        pytest.param(
            {
                'private_key': _PRIVATE_KEY,
                'preshared_key': 'not-base64',
            },
            ValueError,
            id='preshared-key',
        ),
        pytest.param(
            {
                'private_key': _PRIVATE_KEY,
                'addresses': ('not-an-interface',),
            },
            ValueError,
            id='address',
        ),
        pytest.param(
            {
                'private_key': _PRIVATE_KEY,
                'allowed_ips': ('not-a-network',),
            },
            ValueError,
            id='allowed-ip',
        ),
        pytest.param(
            {
                'private_key': _PRIVATE_KEY,
                'listen_port': 65536,
            },
            ValueError,
            id='listen-port',
        ),
        pytest.param(
            {
                'private_key': _PRIVATE_KEY,
                'persistent_keepalive': -1,
            },
            ValueError,
            id='keepalive',
        ),
    ),
)
def test_wg_config_rejects_invalid_values(
    kwargs: dict[str, object],
    error: type[Exception],
) -> None:
    '''
    Invalid config must fail before kernel mutation.

    Parameterize every validated input class and prove direct msgspec
    construction cannot carry malformed configuration into a future
    pyroute2 interface lifecycle.

    '''
    with pytest.raises(error):
        WGInterfaceConfig(**kwargs)  # type: ignore[arg-type]
