'''
Special case testing for issues not (dis)covered in the primary
`Context` related functional/scenario suites.

**NOTE: this mod is a WIP** space for handling
odd/rare/undiscovered/not-yet-revealed faults which either
loudly (ideal case) breakl our supervision protocol
or (worst case) result in distributed sys hangs.

Suites here further try to clarify (if [partially] ill-defined) and
verify our edge case semantics for inter-actor-relayed-exceptions
including,

- lowlevel: what remote obj-data is interchanged for IPC and what is
  native-obj form is expected from unpacking in the the new
  mem-domain.

- which kinds of `RemoteActorError` (and its derivs) are expected by which
  (types of) peers (parent, child, sibling, etc) with what
  particular meta-data set such as,

  - `.src_uid`: the original (maybe) peer who raised.
  - `.relay_uid`: the next-hop-peer who sent it.
  - `.relay_path`: the sequence of peer actor hops.
  - `.is_inception`: a predicate that denotes multi-hop remote errors.

- when should `ExceptionGroup`s be relayed from a particular
  remote endpoint, they should never be caused by implicit `._rpc`
  nursery machinery!

- various special `trio` edge cases around its cancellation semantics
  and how we (currently) leverage `trio.Cancelled` as a signal for
  whether a `Context` task should raise `ContextCancelled` (ctx).

'''
import pytest
import trio
import tractor
from tractor import (  # typing
    ActorNursery,
    Portal,
    Context,
    ContextCancelled,
)


@tractor.context
async def sleep_n_chkpt_in_finally(
    ctx: Context,
    sleep_n_raise: bool,

    chld_raise_delay: float,
    chld_finally_delay: float,

    rent_cancels: bool,
    rent_ctxc_delay: float,

    expect_exc: str|None = None,

) -> None:
    '''
    Sync, open a tn, then wait for cancel, run a chkpt inside
    the user's `finally:` teardown.

    This covers a footgun case that `trio` core doesn't seem to care about
    wherein an exc can be masked by a `trio.Cancelled` raised inside a tn emedded
    `finally:`.

    Also see `test_trioisms::test_acm_embedded_nursery_propagates_enter_err`
    for the down and gritty details.

    Since a `@context` endpoint fn can also contain code like this,
    **and** bc we currently have no easy way other then
    `trio.Cancelled` to signal cancellation on each side of an IPC `Context`,
    the footgun issue can compound itself as demonstrated in this suite..

    Here are some edge cases codified with our WIP "sclang" syntax
    (note the parent(rent)/child(chld) naming here is just
    pragmatism, generally these most of these cases can occurr
    regardless of the distributed-task's supervision hiearchy),

    - rent c)=> chld.raises-then-taskc-in-finally
     |_ chld's body raises an `exc: BaseException`.
      _ in its `finally:` block it runs a chkpoint
        which raises a taskc (`trio.Cancelled`) which
        masks `exc` instead raising taskc up to the first tn.
      _ the embedded/chld tn captures the masking taskc and then
        raises it up to the ._rpc-ep-tn instead of `exc`.
      _ the rent thinks the child ctxc-ed instead of errored..

    '''
    await ctx.started()

    if expect_exc:
        expect_exc: BaseException = tractor._exceptions.get_err_type(
            type_name=expect_exc,
        )

    berr: BaseException|None = None
    try:
        if not sleep_n_raise:
            await trio.sleep_forever()
        elif sleep_n_raise:

            # XXX this sleep is less then the sleep the parent
            # does before calling `ctx.cancel()`
            await trio.sleep(chld_raise_delay)

            # XXX this will be masked by a taskc raised in
            # the `finally:` if this fn doesn't terminate
            # before any ctxc-req arrives AND a checkpoint is hit
            # in that `finally:`.
            raise RuntimeError('my app krurshed..')

    except BaseException as _berr:
        berr = _berr

        # TODO: it'd sure be nice to be able to inject our own
        # `ContextCancelled` here instead of of `trio.Cancelled`
        # so that our runtime can expect it and this "user code"
        # would be able to tell the diff between a generic trio
        # cancel and a tractor runtime-IPC cancel.
        if expect_exc:
            if not isinstance(
                berr,
                expect_exc,
            ):
                raise ValueError(
                    f'Unexpected exc type ??\n'
                    f'{berr!r}\n'
                    f'\n'
                    f'Expected a {expect_exc!r}\n'
                )

        raise berr

    # simulate what user code might try even though
    # it's a known boo-boo..
    finally:
        # maybe wait for rent ctxc to arrive
        with trio.CancelScope(shield=True):
            await trio.sleep(chld_finally_delay)

        # !!XXX this will raise `trio.Cancelled` which
        # will mask the RTE from above!!!
        #
        # YES, it's the same case as our extant
        # `test_trioisms::test_acm_embedded_nursery_propagates_enter_err`
        try:
            await trio.lowlevel.checkpoint()
        except trio.Cancelled as taskc:
            if (scope_err := taskc.__context__):
                print(
                    f'XXX MASKED REMOTE ERROR XXX\n'
                    f'ENDPOINT exception -> {scope_err!r}\n'
                    f'will be masked by -> {taskc!r}\n'
                )
                # await tractor.pause(shield=True)

            raise taskc


@pytest.mark.parametrize(
    'chld_callspec',
    [
        dict(
            sleep_n_raise=None,
            chld_raise_delay=0.1,
            chld_finally_delay=0.1,
            expect_exc='Cancelled',
            rent_cancels=True,
            rent_ctxc_delay=0.1,
        ),
        dict(
            sleep_n_raise='RuntimeError',
            chld_raise_delay=0.1,
            chld_finally_delay=1,
            expect_exc='RuntimeError',
            rent_cancels=False,
            rent_ctxc_delay=0.1,
        ),
    ],
    ids=lambda item: f'chld_callspec={item!r}'
)
def test_unmasked_remote_exc(
    debug_mode: bool,
    chld_callspec: dict,
    tpt_proto: str,
):
    expect_exc_str: str|None = chld_callspec['sleep_n_raise']
    rent_ctxc_delay: float|None = chld_callspec['rent_ctxc_delay']
    async def main():
        an: ActorNursery
        async with tractor.open_nursery(
            debug_mode=debug_mode,
            enable_transports=[tpt_proto],
        ) as an:
            ptl: Portal = await an.start_actor(
                'cancellee',
                enable_modules=[__name__],
            )
            ctx: Context
            async with (
                ptl.open_context(
                    sleep_n_chkpt_in_finally,
                    **chld_callspec,
                ) as (ctx, sent),
            ):
                assert not sent
                await trio.sleep(rent_ctxc_delay)
                await ctx.cancel()

                # recv error or result from chld
                ctxc: ContextCancelled = await ctx.wait_for_result()
                assert (
                    ctxc is ctx.outcome
                    and
                    isinstance(ctxc, ContextCancelled)
                )

            # always graceful terminate the sub in non-error cases
            await an.cancel()

    if expect_exc_str:
        expect_exc: BaseException = tractor._exceptions.get_err_type(
            type_name=expect_exc_str,
        )
        with pytest.raises(
            expected_exception=tractor.RemoteActorError,
        ) as excinfo:
            trio.run(main)

        rae = excinfo.value
        assert expect_exc == rae.boxed_type

    else:
        trio.run(main)
