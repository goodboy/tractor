'''
Process-local struct wire-encoding guards.

'''
from __future__ import annotations

import msgspec
import pytest

from tractor.msg import ProcessLocal


class LocalHandle(ProcessLocal):
    '''
    Minimal process-local struct used to exercise the global marker.

    '''
    resource_id: int


@pytest.mark.parametrize(
    'nested',
    (
        pytest.param(
            False,
            id='direct',
        ),
        pytest.param(
            True,
            id='nested',
        ),
    ),
)
def test_process_local_rejects_default_encoding(
    nested: bool,
) -> None:
    '''
    Process-local values can appear directly or deep in a payload.

    Embed the same marked struct at both depths and prove msgspec's
    normal traversal reaches the unsupported sentinel without a
    tractor-specific recursive payload scan.

    '''
    handle: LocalHandle = LocalHandle(resource_id=1)
    value: object = (
        {'nested': [handle]}
        if nested
        else handle
    )

    assert repr(handle) == 'LocalHandle(resource_id=1)'
    with pytest.raises(
        TypeError,
        match='_ProcessLocalToken.*unsupported',
    ):
        msgspec.msgpack.encode(value)
