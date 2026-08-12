# tractor: structured concurrent "actors".
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

'''
A shm-thread-as-generator-fn-ctx **prototype** in anticipation of
free-threading (aka GIL-less threads) in py 3.13+

Bo

Main rationale,
- binding a bg-thread to a "suspendable fn scope" means avoiding any
  locking around shm-data-structures and, except for a single
  `threading.Condition` (or better) for thread-context-switching,
  enables a "pure-ish" style semantic for inter-thread
  value-passing-IO between a "parent" and (bg) "child" shm-thread
  where the only (allowed) data-flow would be via immutable values in
  a "coroutine-style" using,

  - parent-side:
   |_ `callee_sent = gen.send(caller_sent)`

  - child-side:
   |_ `caller_sent = yield callee_sent`

Related (official) reading,
- https://docs.python.org/3/glossary.html#term-free-threading
- https://peps.python.org/pep-0703/
 |_https://peps.python.org/pep-0703/#optimistic-avoiding-locking-in-dict-and-list-accesses


'''
from contextlib import (
    contextmanager as cm,
)
import inspect
# from functools import partial
import time
import threading
from typing import (
    Any,
    Generator,
)

import tractor
import trio


log = tractor.log.get_console_log(
    'info',
    # ^ XXX causes latency with seed>=1e3
    # 'warning',
)
_seed: int = int(1e3)


def thread_gen(seed: int):
    thr = threading.current_thread()
    log.info(
        f'thr: {thr.name} @ {thr.ident}\n'
        f'    |_{thr!r}\n'
        f'\n'
        f'IN `thread_gen(seed={seed})`\n'
        f'\n'
        f'Starting range()-loop\n'
    )
    for i in range(seed):
        log.info(
            f'yielding i={i}\n'
        )
        from_main = yield i
        log.info(
            f'(from_main := {from_main}) = yield (i:={i})\n'
        )
        # time.sleep(0.0001)


# TODO, how would we get the equiv from a pub trio-API?
# -[ ] what about an inter-thread channel much like we have for
# `to_asyncio` & guest mode??
#
# async def spawn_bg_thread_running_gen(fn):
#     log.info('running trio.to_thread.run_sync()')
#     await trio.to_thread.run_sync(
#        partial(
#          run_gen_in_thread,
#          fn=fn,
#          seed=_seed,
#         )
#     )


# ?TODO? once correct, wrap this as a @deco-API?
# -[ ] @generator_thread or similar?
#
def run_gen_in_thread(
    cond: threading.Condition,
    gen: Generator,
    # ^NOTE, already closure-bound-in tgt generator-fn-instance which
    # will be yielded to in the bg-thread!
):
    thr: threading.Thread = threading.current_thread()
    log.info(
        f'thr: {thr.name} @ {thr.ident}\n'
        f'    |_{thr!r}\n'
        f'\n'
        f'IN `run_gen_in_thread(gen={gen})`\n'
        f'\n'
        f'entering gen blocking: {gen!r}\n'
    )
    try:
        log.runtime('locking cond..')
        with cond:
            log.runtime('LOCKED cond..')
            first_yielded = gen.send(None)
            assert cond.to_yield is None
            cond.to_yield = first_yielded
            log.runtime('notifying cond..')
            cond.notify()
            log.runtime('waiting cond..')
            cond.wait()

            while (to_send := cond.to_send) is not None:
                try:
                    yielded = gen.send(to_send)
                except StopIteration as siter:
                    # TODO, check for return value?
                    # if (ret := siter.value):
                    #     cond.to_return = ret
                    assert siter
                    log.exception(f'{gen} exited')
                    raise

                cond.to_yield = yielded
                log.runtime('LOOP notifying cond..')
                cond.notify()
                log.runtime('LOOP waiting cond..')
                cond.wait()

            # out = (yield from gen)
            log.runtime('RELEASE-ing cond..')

        # with cond block-end
        log.runtime('RELEASE-ed cond..')

    except BaseException:
        log.exception(f'exited gen: {gen!r}\n')
        raise

    finally:
        log.warning(
            'Exiting bg thread!\n'
        )
        # TODO! better then this null setting naivety!
        # -[ ] maybe an Unresolved or similar like for our `Context`?
        #
        # apply sentinel
        cond.to_yield = None
        with cond:
            cond.notify_all()

@cm
def start_in_bg_thread(
    gen: Generator,

    # ?TODO?, is this useful to pass startup-ctx to the thread?
    name: str|None = None,
    **kwargs,

) -> tuple[
    threading.Thread,
    Generator,
    Any,
]:
    if not inspect.isgenerator(gen):
        raise ValueError(
            f'You must pass a `gen: Generator` instance\n'
            f'gen={gen!r}\n'
        )

    # ?TODO? wrap this stuff into some kinda
    # single-entry-inter-thread mem-chan?
    #
    cond = threading.Condition()
    cond.to_send = None
    cond.to_yield = None
    cond.to_return = None

    thr = threading.Thread(
        target=run_gen_in_thread,
        # args=(),  # ?TODO, useful?
        kwargs={
            'cond': cond,
            'gen': gen,
        } | kwargs,
        name=name or gen.__name__,
    )
    log.info(
        f'starting bg thread\n'
        f'>(\n'
        f'|_{thr!r}\n'
    )
    thr.start()

    # TODO, Event or cond.wait() here to sync!?
    time.sleep(0.01)

    try:
        log.info(f'locking cond {cond}..')
        with cond:
            log.runtime(f'LOCKED cond {cond}..')
            first_yielded = cond.to_yield
            log.runtime(f'cond.to_yield: {first_yielded}')

            # delegator shim generator which proxies values from
            # caller to callee-in-bg-thread
            def wrapper():

                # !?TODO, minimize # of yields during startup?
                # -[ ] we can do i in <=1 manual yield pre while-loop no?
                #
                first_sent = yield first_yielded
                cond.to_send = first_sent

                # !TODO, exactly why we need a conditional-emit-sys!
                log.runtime(
                    f'cond.notify()\n'
                    f'cond.to_send={cond.to_send!r}\n'
                    f'cond.to_yield={cond.to_yield!r}\n'
                )
                cond.notify()
                log.runtime(
                    f'cond.wait()\n'
                    f'cond.to_send={cond.to_send!r}\n'
                    f'cond.to_yield={cond.to_yield!r}\n'
                )
                cond.wait()

                to_yield = cond.to_yield
                log.runtime(
                    f'yielding to caller\n'
                    f'cond.to_send={cond.to_send!r}\n'
                    f'cond.to_yield={cond.to_yield!r}\n'
                )
                to_send = yield to_yield
                log.runtime(
                    f'post-yield to caller\n'
                    f'to_send={to_send!r}\n'
                    f'to_yield={to_yield!r}\n'
                )

                # !TODO, proper sentinel-to-break type-condition!
                while to_send is not None:
                    cond.to_send = to_send
                    log.runtime(
                        f'cond.nofity()\n'
                        f'cond.to_send={cond.to_send!r}\n'
                        f'cond.to_yield={cond.to_yield!r}\n'
                    )
                    cond.notify()
                    if cond.to_yield is None:
                        log.runtime(
                            'BREAKING from wrapper-LOOP!\n'
                        )
                        break
                        return

                    log.runtime(
                        f'cond.wait()\n'
                        f'cond.to_send={cond.to_send!r}\n'
                        f'cond.to_yield={cond.to_yield!r}\n'
                    )
                    cond.wait()

                    log.runtime(
                        f'yielding to caller\n'
                        f'cond.to_send={cond.to_send!r}\n'
                        f'cond.to_yield={cond.to_yield!r}\n'
                    )
                    to_yield = cond.to_yield
                    to_send = yield to_yield
                    log.runtime(
                        f'post-yield to caller\n'
                        f'to_send={to_send!r}\n'
                        f'to_yield={to_yield!r}\n'
                    )

            log.info('creating wrapper..')
            wrapper_gen = wrapper()
            log.info(f'first .send(None): {wrapper_gen}\n')
            first_yielded = wrapper_gen.send(None)
            log.info(f'first yielded: {first_yielded}\n')

            yield (
                thr,
                wrapper_gen,
                first_yielded,
            )
    finally:
        thr.join()
        log.info(f'bg thread joined: {thr!r}')


async def main():
    async with trio.open_nursery() as tn:
        assert tn

        with (
            start_in_bg_thread(
                gen=(
                    _gen:=thread_gen(
                        seed=_seed,
                    )
                ),
            ) as (
                thr,
                wrapped_gen,
                first,
            ),
        ):
            assert (
                _gen is not wrapped_gen
                and
                wrapped_gen is not None
            )
            log.info(
                'Entering wrapped_gen loop\n'
            )

            # NOTE, like our `Context.started` value
            assert first == 0

            # !TODO, proper sentinel-to-break type-condition!
            yielded = first
            while yielded is not None:

                # XXX, compute callers new value to send to bg-thread
                to_send = yielded * yielded

                # send to bg-thread
                yielded = wrapped_gen.send(to_send)
                log.info(
                    f'(yielded:={yielded!r}) = wrapped_gen.send((to_send:={to_send!r})'
                )


if __name__ == '__main__':
    trio.run(main)
