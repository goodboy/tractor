import trio
import tractor


async def main():
    async with tractor.open_root_actor(
        debug_mode=True,
        loglevel='cancel',
    ) as _root:

        # manually trigger self-cancellation and wait
        # for it to fully trigger.
        _root.cancel_soon()
        await _root._cancel_complete.wait()
        print('root cancelled')

        # now ensure we can still use the REPL
        try:
            await tractor.pause()
        except trio.Cancelled as _taskc:
            assert (root_cs := _root._root_tn.cancel_scope).cancel_called
            # NOTE^^ above logic but inside `open_root_actor()` and
            # passed to the `shield=` expression is effectively what
            # we're testing here!
            await tractor.pause(shield=root_cs.cancel_called)

        # XXX, if shield logic *is wrong* inside `open_root_actor()`'s
        # crash-handler block this should never be interacted,
        # instead `trio.Cancelled` would be bubbled up: the original
        # BUG.
        assert 0


if __name__ == '__main__':
    trio.run(main)
