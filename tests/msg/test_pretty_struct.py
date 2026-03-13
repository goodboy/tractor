'''
Unit tests for `tractor.msg.pretty_struct`
private-field filtering in `pformat()`.

'''
import pytest

from tractor.msg.pretty_struct import (
    Struct,
    pformat,
    iter_struct_ppfmt_lines,
)
from tractor.msg._codec import (
    MsgDec,
    mk_dec,
)


# ------ test struct definitions ------ #

class PublicOnly(Struct):
    '''
    All-public fields for baseline testing.

    '''
    name: str = 'alice'
    age: int = 30


class PrivateOnly(Struct):
    '''
    Only underscore-prefixed (private) fields.

    '''
    _secret: str = 'hidden'
    _internal: int = 99


class MixedFields(Struct):
    '''
    Mix of public and private fields.

    '''
    name: str = 'bob'
    _hidden: int = 42
    value: float = 3.14
    _meta: str = 'internal'


class Inner(
    Struct,
    frozen=True,
):
    '''
    Frozen inner struct with a private field,
    for nesting tests.

    '''
    x: int = 1
    _secret: str = 'nope'


class Outer(Struct):
    '''
    Outer struct nesting an `Inner`.

    '''
    label: str = 'outer'
    inner: Inner = Inner()


class EmptyStruct(Struct):
    '''
    Struct with zero fields.

    '''
    pass


# ------ tests ------ #

@pytest.mark.parametrize(
    'struct_and_expected',
    [
        (
            PublicOnly(),
            {
                'shown': ['name', 'age'],
                'hidden': [],
            },
        ),
        (
            MixedFields(),
            {
                'shown': ['name', 'value'],
                'hidden': ['_hidden', '_meta'],
            },
        ),
        (
            PrivateOnly(),
            {
                'shown': [],
                'hidden': ['_secret', '_internal'],
            },
        ),
    ],
    ids=[
        'all-public',
        'mixed-pub-priv',
        'all-private',
    ],
)
def test_field_visibility_in_pformat(
    struct_and_expected: tuple[
        Struct,
        dict[str, list[str]],
    ],
):
    '''
    Verify `pformat()` shows public fields
    and hides `_`-prefixed private fields.

    '''
    (
        struct,
        expected,
    ) = struct_and_expected
    output: str = pformat(struct)

    for field_name in expected['shown']:
        assert field_name in output, (
            f'{field_name!r} should appear in:\n'
            f'{output}'
        )

    for field_name in expected['hidden']:
        assert field_name not in output, (
            f'{field_name!r} should NOT appear in:\n'
            f'{output}'
        )


def test_iter_ppfmt_lines_skips_private():
    '''
    Directly verify `iter_struct_ppfmt_lines()`
    never yields tuples with `_`-prefixed field
    names.

    '''
    struct = MixedFields()
    lines: list[tuple[str, str]] = list(
        iter_struct_ppfmt_lines(
            struct,
            field_indent=2,
        )
    )
    # should have lines for public fields only
    assert len(lines) == 2

    for _prefix, line_content in lines:
        field_name: str = (
            line_content.split(':')[0].strip()
        )
        assert not field_name.startswith('_'), (
            f'private field leaked: {field_name!r}'
        )


def test_nested_struct_filters_inner_private():
    '''
    Verify that nested struct's private fields
    are also filtered out during recursion.

    '''
    outer = Outer()
    output: str = pformat(outer)

    # outer's public field
    assert 'label' in output

    # inner's public field (recursed into)
    assert 'x' in output

    # inner's private field must be hidden
    assert '_secret' not in output


def test_empty_struct_pformat():
    '''
    An empty struct should produce a valid
    `pformat()` result with no field lines.

    '''
    output: str = pformat(EmptyStruct())
    assert 'EmptyStruct(' in output
    assert output.rstrip().endswith(')')

    # no field lines => only struct header+footer
    lines: list[tuple[str, str]] = list(
        iter_struct_ppfmt_lines(
            EmptyStruct(),
            field_indent=2,
        )
    )
    assert lines == []


def test_real_msgdec_pformat_hides_private():
    '''
    Verify `pformat()` on a real `MsgDec`
    hides the `_dec` internal field.

    NOTE: `MsgDec.__repr__` is custom and does
    NOT call `pformat()`, so we call it directly.

    '''
    dec: MsgDec = mk_dec(spec=int)
    output: str = pformat(dec)

    # the private `_dec` field should be filtered
    assert '_dec' not in output

    # but the struct type name should be present
    assert 'MsgDec(' in output


def test_pformat_repr_integration():
    '''
    Verify that `Struct.__repr__()` (which calls
    `pformat()`) also hides private fields for
    custom structs that do NOT override `__repr__`.

    '''
    mixed = MixedFields()
    output: str = repr(mixed)

    assert 'name' in output
    assert 'value' in output
    assert '_hidden' not in output
    assert '_meta' not in output
