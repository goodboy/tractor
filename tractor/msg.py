"""
Messaging pattern APIs and helpers.
"""
import typing
from typing import Dict, Any
from functools import partial

import trio
import wrapt

from .log import get_logger
from . import current_actor

__all__ = ['pub']

log = get_logger('messaging')


async def fan_out_to_ctxs(
    pub_gen: typing.Callable,  # it's an async gen ... gd mypy
    topics2ctxs: Dict[str, set],
    topic_key: str = 'key',
) -> None:
    """Request and fan out quotes to each subscribed actor channel.
    """
    def get_topics():
        return tuple(topics2ctxs.keys())

    async for published in pub_gen(
        get_topics=get_topics,
    ):
        ctx_payloads: Dict[str, Any] = {}
        for packet_key, data in published.items():
            # grab each suscription topic using provided key for lookup
            topic = data[topic_key]
            # build a new dict packet for passing to multiple channels
            packet = {packet_key: data}
            for ctx in topics2ctxs.get(topic, set()):
                ctx_payloads.setdefault(ctx, {}).update(packet),

        # deliver to each subscriber (fan out)
        if ctx_payloads:
            for ctx, payload in ctx_payloads.items():
                try:
                    await ctx.send_yield(payload)
                except (
                    # That's right, anything you can think of...
                    trio.ClosedStreamError, ConnectionResetError,
                    ConnectionRefusedError,
                ):
                    log.warn(f"{ctx.chan} went down?")
                    for ctx_set in topics2ctxs.values():
                        ctx_set.discard(ctx)

        if not any(topics2ctxs.values()):
            log.warn(f"No subscribers left for {pub_gen.__name__}")
            break


def modify_subs(topics2ctxs, topics, ctx):
    """Absolute symbol subscription list for each quote stream.

    Effectively a symbol subscription api.
    """
    log.info(f"{ctx.chan} changed subscription to {topics}")

    # update map from each symbol to requesting client's chan
    for ticker in topics:
        topics2ctxs.setdefault(ticker, set()).add(ctx)

    # remove any existing symbol subscriptions if symbol is not
    # found in ``symbols``
    # TODO: this can likely be factored out into the pub-sub api
    for ticker in filter(
        lambda topic: topic not in topics, topics2ctxs.copy()
    ):
        ctx_set = topics2ctxs.get(ticker)
        ctx_set.discard(ctx)

        if not ctx_set:
            # pop empty sets which will trigger bg quoter task termination
            topics2ctxs.pop(ticker)


def pub(*, tasks=()):
    """Publisher async generator decorator.

    A publisher can be called many times from different actor's
    remote tasks but will only spawn one internal task to deliver
    values to all callers. Values yielded from the decorated
    async generator are sent back to each calling task, filtered by
    topic on the producer (server) side.

    Must be called with a topic as the first arg.
    """
    task2lock = {}
    for name in tasks:
        task2lock[name] = trio.StrictFIFOLock()

    @wrapt.decorator
    async def wrapper(agen, instance, args, kwargs):
        if tasks:
            task_key = kwargs.pop('task_key')
            if not task_key:
                raise TypeError(
                    f"{agen} must be called with a `task_key` named argument "
                    f"with a falue from {tasks}")

        # pop required kwargs used internally
        ctx = kwargs.pop('ctx')
        topics = kwargs.pop('topics')
        topic_key = kwargs.pop('topic_key')

        lock = task2lock[task_key]
        ss = current_actor().statespace
        all_subs = ss.setdefault('_subs', {})
        topics2ctxs = all_subs.setdefault(task_key, {})

        try:
            modify_subs(topics2ctxs, topics, ctx)
            # block and let existing feed task deliver
            # stream data until it is cancelled in which case
            # we'll take over and spawn it again
            # update map from each symbol to requesting client's chan
            async with lock:
                # no data feeder task yet; so start one
                respawn = True
                while respawn:
                    respawn = False
                    log.info(f"Spawning data feed task for {agen.__name__}")
                    try:
                        # unblocks when no more symbols subscriptions exist
                        # and the streamer task terminates
                        await fan_out_to_ctxs(
                            pub_gen=partial(agen, *args, **kwargs),
                            topics2ctxs=topics2ctxs,
                            topic_key=topic_key,
                        )
                        log.info(f"Terminating stream task for {task_key}"
                                 f" for {agen.__name__}")
                    except trio.BrokenResourceError:
                        log.exception("Respawning failed data feed task")
                        respawn = True
        finally:
            # remove all subs for this context
            modify_subs(topics2ctxs, (), ctx)

            # if there are truly no more subscriptions with this broker
            # drop from broker subs dict
            if not any(topics2ctxs.values()):
                log.info(f"No more subscriptions for publisher {task_key}")

    return wrapper
