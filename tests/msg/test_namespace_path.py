'''
`NamespacePath` Python-object reference tests.

'''
import pytest

from tractor.msg import ptr as msgptr
from tractor.msg.ptr import NamespacePath


def example_target() -> None:
    '''
    Provide a module-addressable reference for pointer tests.

    '''


def test_retains_target_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    '''
    Reuse a retained target ref when splitting its namespace path.

    `NamespacePath.from_ref()` previously discarded `example_target`,
    so `to_tuple()` imported and resolved the just-created string again.
    Replacing `resolve_name()` with a failure proves the retained ref
    supplies the tuple without a redundant lookup.

    '''
    target = NamespacePath.from_ref(example_target)

    def fail_resolve(name: str) -> object:
        raise AssertionError(f'unexpected lookup for {name!r}')

    monkeypatch.setattr(
        msgptr,
        'resolve_name',
        fail_resolve,
    )
    assert target.to_tuple() == (
        example_target.__module__,
        example_target.__name__,
    )
