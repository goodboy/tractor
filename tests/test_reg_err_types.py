'''
Verify that externally registered remote actor error
types are correctly relayed, boxed, and re-raised across
IPC actor hops via `reg_err_types()`.

Also ensure that when custom error types are NOT registered
the framework indicates the lookup failure to the user.

'''
import pytest
import trio
import tractor
from tractor import (
    Context,
    Portal,
    RemoteActorError,
)
from tractor._exceptions import (
    get_err_type,
    reg_err_types,
)


# -- custom app-level errors for testing --
class CustomAppError(Exception):
    '''
    A hypothetical user-app error that should be
    boxed+relayed by `tractor` IPC when registered.

    '''


class AnotherAppError(Exception):
    '''
    A second custom error for multi-type registration.

    '''


class UnregisteredAppError(Exception):
    '''
    A custom error that is intentionally NEVER
    registered via `reg_err_types()` so we can
    verify the framework's failure indication.

    '''


# -- remote-task endpoints --
@tractor.context
async def raise_custom_err(
    ctx: Context,
) -> None:
    '''
    Remote ep that raises a `CustomAppError`
    after sync-ing with the caller.

    '''
    await ctx.started()
    raise CustomAppError(
        'the app exploded remotely'
    )


@tractor.context
async def raise_another_err(
    ctx: Context,
) -> None:
    '''
    Remote ep that raises `AnotherAppError`.

    '''
    await ctx.started()
    raise AnotherAppError(
        'another app-level kaboom'
    )


@tractor.context
async def raise_unreg_err(
    ctx: Context,
) -> None:
    '''
    Remote ep that raises an `UnregisteredAppError`
    which has NOT been `reg_err_types()`-registered.

    '''
    await ctx.started()
    raise UnregisteredAppError(
        'this error type is unknown to tractor'
    )


# -- unit tests for the type-registry plumbing --

class TestRegErrTypesPlumbing:
    '''
    Low-level checks on `reg_err_types()` and
    `get_err_type()` without requiring IPC.

    '''

    def test_unregistered_type_returns_none(self):
        '''
        An unregistered custom error name should yield
        `None` from `get_err_type()`.

        '''
        result = get_err_type('CustomAppError')
        assert result is None

    def test_register_and_lookup(self):
        '''
        After `reg_err_types()`, the custom type should
        be discoverable via `get_err_type()`.

        '''
        reg_err_types([CustomAppError])
        result = get_err_type('CustomAppError')
        assert result is CustomAppError

    def test_register_multiple_types(self):
        '''
        Registering a list of types should make each
        one individually resolvable.

        '''
        reg_err_types([
            CustomAppError,
            AnotherAppError,
        ])
        assert (
            get_err_type('CustomAppError')
            is CustomAppError
        )
        assert (
            get_err_type('AnotherAppError')
            is AnotherAppError
        )

    def test_builtin_types_always_resolve(self):
        '''
        Builtin error types like `RuntimeError` and
        `ValueError` should always be found without
        any prior registration.

        '''
        assert (
            get_err_type('RuntimeError')
            is RuntimeError
        )
        assert (
            get_err_type('ValueError')
            is ValueError
        )

    def test_tractor_native_types_resolve(self):
        '''
        `tractor`-internal exc types (e.g.
        `ContextCancelled`) should always resolve.

        '''
        assert (
            get_err_type('ContextCancelled')
            is tractor.ContextCancelled
        )

    def test_boxed_type_str_without_ipc_msg(self):
        '''
        When a `RemoteActorError` is constructed
        without an IPC msg (and no resolvable type),
        `.boxed_type_str` should return `'<unknown>'`.

        '''
        rae = RemoteActorError('test')
        assert rae.boxed_type_str == '<unknown>'


# -- IPC-level integration tests --

def test_registered_custom_err_relayed(
    debug_mode: bool,
    tpt_proto: str,
):
    '''
    When a custom error type is registered via
    `reg_err_types()` on BOTH sides of an IPC dialog,
    the parent should receive a `RemoteActorError`
    whose `.boxed_type` matches the original custom
    error type.

    '''
    reg_err_types([CustomAppError])

    async def main():
        async with tractor.open_nursery(
            debug_mode=debug_mode,
            enable_transports=[tpt_proto],
        ) as an:
            ptl: Portal = await an.start_actor(
                'custom-err-raiser',
                enable_modules=[__name__],
            )
            async with ptl.open_context(
                raise_custom_err,
            ) as (ctx, sent):
                assert not sent
                try:
                    await ctx.wait_for_result()
                except RemoteActorError as rae:
                    assert rae.boxed_type is CustomAppError
                    assert rae.src_type is CustomAppError
                    assert 'the app exploded remotely' in str(
                        rae.tb_str
                    )
                    raise

            await an.cancel()

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    rae = excinfo.value
    assert rae.boxed_type is CustomAppError


def test_registered_another_err_relayed(
    debug_mode: bool,
    tpt_proto: str,
):
    '''
    Same as above but for a different custom error
    type to verify multi-type registration works
    end-to-end over IPC.

    '''
    reg_err_types([AnotherAppError])

    async def main():
        async with tractor.open_nursery(
            debug_mode=debug_mode,
            enable_transports=[tpt_proto],
        ) as an:
            ptl: Portal = await an.start_actor(
                'another-err-raiser',
                enable_modules=[__name__],
            )
            async with ptl.open_context(
                raise_another_err,
            ) as (ctx, sent):
                assert not sent
                try:
                    await ctx.wait_for_result()
                except RemoteActorError as rae:
                    assert (
                        rae.boxed_type
                        is AnotherAppError
                    )
                    raise

            await an.cancel()

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    rae = excinfo.value
    assert rae.boxed_type is AnotherAppError


def test_unregistered_err_still_relayed(
    debug_mode: bool,
    tpt_proto: str,
):
    '''
    Verify that even when a custom error type is NOT registered via
    `reg_err_types()`, the remote error is still relayed as
    a `RemoteActorError` with all string-level info preserved
    (traceback, type name, source actor uid).

    The `.boxed_type` will be `None` (type obj can't be resolved) but
    `.boxed_type_str` and `.src_type_str` still report the original
    type name from the IPC msg.

    This document the expected limitation: without `reg_err_types()`
    the `.boxed_type` property can NOT resolve to the original Python
    type.

    '''
    # NOTE: intentionally do NOT call
    # `reg_err_types([UnregisteredAppError])`

    async def main():
        async with tractor.open_nursery(
            debug_mode=debug_mode,
            enable_transports=[tpt_proto],
        ) as an:
            ptl: Portal = await an.start_actor(
                'unreg-err-raiser',
                enable_modules=[__name__],
            )
            async with ptl.open_context(
                raise_unreg_err,
            ) as (ctx, sent):
                assert not sent
                await ctx.wait_for_result()

            await an.cancel()

    with pytest.raises(RemoteActorError) as excinfo:
        trio.run(main)

    rae = excinfo.value

    # the error IS relayed even without
    # registration; type obj is unresolvable but
    # all string-level info is preserved.
    assert rae.boxed_type is None # NOT `UnregisteredAppError`
    assert rae.src_type is None

    # string names survive the IPC round-trip
    # via the `Error` msg fields.
    assert (
        rae.src_type_str
        ==
        'UnregisteredAppError'
    )
    assert (
        rae.boxed_type_str
        ==
        'UnregisteredAppError'
    )

    # original traceback content is preserved
    assert 'this error type is unknown' in rae.tb_str
    assert 'UnregisteredAppError' in rae.tb_str
