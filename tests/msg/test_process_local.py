'''
Process-local struct wire-encoding guards.

'''
from __future__ import annotations

import msgspec
import pytest

from tractor.msg import (
    FrozenProcessLocal,
    ProcessLocal,
)


class LocalHandle(ProcessLocal):
    '''
    Default marker whose sentinel must fail direct and nested
    encoding.

    '''
    resource_id: int


class FrozenLocalHandle(FrozenProcessLocal):
    '''
    Frozen outer fields whose referenced `labels` list remains
    mutable.

    Supplying `_process_local=None` would also replace the
    unsupported sentinel with an encodable value if construction did
    not reject it.

    '''
    resource_id: int
    # Mutating this list distinguishes shallow struct freezing from
    # recursively freezing every referenced object.
    labels: list[str]


# This class option would omit the default-valued sentinel from
# msgpack.
class OmittingLocalHandle(
    ProcessLocal,
    omit_defaults=True,
):
    '''
    Encoder config which would drop the default-valued sentinel.

    With `omit_defaults=True`, both `_process_local` and
    `resource_id`
    equal their defaults, so msgspec could encode this struct as an
    empty map without ever traversing `_ProcessLocalToken`.

    '''
    resource_id: int = 1


class ValidatedLocalHandle(ProcessLocal):
    '''
    Subclass validator which would shadow the marker's post-init
    hook.

    `ProcessLocal.__init_subclass__()` must wrap this method so
    sentinel validation still runs without requiring a cooperative
    `super()` call, while retaining this resource-id validation.

    '''
    resource_id: int

    # Defining this hook normally shadows an inherited
    # `__post_init__`; `ProcessLocal` must wrap it rather than rely
    # on a `super()` call.
    def __post_init__(self) -> None:
        if self.resource_id < 0:
            raise ValueError('resource_id must be non-negative')


class MutatingFrozenLocalHandle(FrozenProcessLocal):
    '''
    Hook which uses msgspec's frozen-field escape hatch on the
    sentinel.

    A check only before this hook would miss the replacement and
    leave the completed struct encodable, so the wrapper must check
    again after subclass validation.

    '''
    resource_id: int

    # Replacing the sentinel inside this hook defeats a pre-hook-only
    # check even though the struct is frozen.
    def __post_init__(self) -> None:
        # This is msgspec's supported internal mutation path for
        # frozen structs, and therefore the strongest sentinel-
        # replacement case.
        msgspec.structs.force_setattr(
            self,
            '_process_local',
            None,
        )


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


def test_frozen_process_local_contract() -> None:
    '''
    Preserve wire rejection while freezing process-local struct
    fields.

    A plain frozen msgspec struct could otherwise cross actor IPC.
    Build one `FrozenProcessLocal` with a mutable referenced list,
    then prove direct and nested default encoding still reach
    `_ProcessLocalToken`. Reject field replacement while allowing
    mutation owned by the nested list, demonstrating that the marker
    provides shallow immutability.

    '''
    handle = FrozenLocalHandle(
        resource_id=1,
        labels=['initial'],
    )

    for value in (
        handle,
        {'nested': [handle]},
    ):
        with pytest.raises(
            TypeError,
            match='_ProcessLocalToken.*unsupported',
        ):
            msgspec.msgpack.encode(value)

    with pytest.raises(AttributeError):
        handle.resource_id = 2  # type: ignore[misc]

    handle.labels.append('changed')
    assert handle.labels == ['initial', 'changed']


def test_process_local_rejects_sentinel_bypasses() -> None:
    '''
    Prevent constructor and encoder options from removing the
    sentinel.

    `FrozenLocalHandle` supplies an encodable sentinel replacement;
    `OmittingLocalHandle` asks msgspec to omit the default sentinel;
    `ValidatedLocalHandle` shadows the inherited post-init hook; and
    `MutatingFrozenLocalHandle` replaces the sentinel from inside
    that hook. Prove construction rejects each wire-safety bypass
    while the ordinary subclass value validator still executes.

    '''
    with pytest.raises(TypeError, match='internal sentinel'):
        FrozenLocalHandle(
            resource_id=1,
            labels=[],
            _process_local=None,  # type: ignore[arg-type]
        )

    with pytest.raises(TypeError, match='omit_defaults'):
        OmittingLocalHandle()

    with pytest.raises(TypeError, match='internal sentinel'):
        ValidatedLocalHandle(
            resource_id=1,
            _process_local=None,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match='must be non-negative'):
        ValidatedLocalHandle(resource_id=-1)

    with pytest.raises(TypeError, match='internal sentinel'):
        MutatingFrozenLocalHandle(resource_id=1)
