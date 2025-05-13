from functools import partial
import time

import trio
import tractor

# TODO: only import these when not running from test harness?
# can we detect `pexpect` usage maybe?
# from tractor.devx.debug import (
#     get_lock,
#     get_debug_req,
# )


def sync_pause(
    use_builtin: bool = False,
    error: bool = False,
    hide_tb: bool = True,
    pre_sleep: float|None = None,
):
    if pre_sleep:
        time.sleep(pre_sleep)

    if use_builtin:
        breakpoint(hide_tb=hide_tb)

    else:
        # TODO: maybe for testing some kind of cm style interface
        # where the `._set_trace()` call doesn't happen until block
        # exit?
        # assert get_lock().ctx_in_debug is None
        # assert get_debug_req().repl is None
        tractor.pause_from_sync()
        # assert get_debug_req().repl is None

    if error:
        raise RuntimeError('yoyo sync code error')


@tractor.context
async def start_n_sync_pause(
    ctx: tractor.Context,
):
    actor: tractor.Actor = tractor.current_actor()

    # sync to parent-side task
    await ctx.started()

    print(f'Entering `sync_pause()` in subactor: {actor.uid}\n')
    sync_pause()
    print(f'Exited `sync_pause()` in subactor: {actor.uid}\n')


async def main() -> None:
    async with (
        tractor.open_nursery(
            debug_mode=True,
            maybe_enable_greenback=True,
            enable_stack_on_sig=True,
            # loglevel='warning',
            # loglevel='devx',
        ) as an,
        trio.open_nursery() as tn,
    ):
        # just from root task
        sync_pause()

        p: tractor.Portal  = await an.start_actor(
            'subactor',
            enable_modules=[__name__],
            # infect_asyncio=True,
            debug_mode=True,
        )

        # TODO: 3 sub-actor usage cases:
        # -[x] via a `.open_context()`
        # -[ ] via a `.run_in_actor()` call
        # -[ ] via a `.run()`
        # -[ ] via a `.to_thread.run_sync()` in subactor
        async with p.open_context(
            start_n_sync_pause,
        ) as (ctx, first):
            assert first is None

            # TODO: handle bg-thread-in-root-actor special cases!
            #
            # there are a couple very subtle situations possible here
            # and they are likely to become more important as cpython
            # moves to support no-GIL.
            #
            # Cases:
            # 1. root-actor bg-threads that call `.pause_from_sync()`
            #   whilst an in-tree subactor also is using ` .pause()`.
            # |_ since the root-actor bg thread can not
            #   `Lock._debug_lock.acquire_nowait()` without running
            #   a `trio.Task`, AND because the
            #   `PdbREPL.set_continue()` is called from that
            #   bg-thread, we can not `._debug_lock.release()`
            #   either!
            #  |_ this results in no actor-tree `Lock` being used
            #    on behalf of the bg-thread and thus the subactor's
            #    task and the thread trying to to use stdio
            #    simultaneously which results in the classic TTY
            #    clobbering!
            #
            # 2. mutiple sync-bg-threads that call
            #   `.pause_from_sync()` where one is scheduled via
            #   `Nursery.start_soon(to_thread.run_sync)` in a bg
            #   task.
            #
            #   Due to the GIL, the threads never truly try to step
            #   through the REPL simultaneously, BUT their `logging`
            #   and traceback outputs are interleaved since the GIL
            #   (seemingly) on every REPL-input from the user
            #   switches threads..
            #
            #   Soo, the context switching semantics of the GIL
            #   result in a very confusing and messy interaction UX
            #   since eval and (tb) print output is NOT synced to
            #   each REPL-cycle (like we normally make it via
            #   a `.set_continue()` callback triggering the
            #   `Lock.release()`). Ideally we can solve this
            #   usability issue NOW because this will of course be
            #   that much more important when eventually there is no
            #   GIL!

            # XXX should cause double REPL entry and thus TTY
            # clobbering due to case 1. above!
            tn.start_soon(
                partial(
                    trio.to_thread.run_sync,
                    partial(
                        sync_pause,
                        use_builtin=False,
                        # pre_sleep=0.5,
                    ),
                    abandon_on_cancel=True,
                    thread_name='start_soon_root_bg_thread',
                )
            )

            await tractor.pause()

            # XXX should cause double REPL entry and thus TTY
            # clobbering due to case 2. above!
            await trio.to_thread.run_sync(
                partial(
                    sync_pause,
                    # NOTE this already works fine since in the new
                    # thread the `breakpoint()` built-in is never
                    # overloaded, thus NO locking is used, HOWEVER
                    # the case 2. from above still exists!
                    use_builtin=True,
                ),
                # TODO: with this `False` we can hang!??!
                # abandon_on_cancel=False,
                abandon_on_cancel=True,
                thread_name='inline_root_bg_thread',
            )

        await ctx.cancel()

        # TODO: case where we cancel from trio-side while asyncio task
        # has debugger lock?
        await p.cancel_actor()


if __name__ == '__main__':
    trio.run(main)
