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
Ring buffer ipc publish-subscribe mechanism brokered by ringd
can dynamically add new outputs (publisher) or inputs (subscriber)
'''
from typing import (
    TypeVar,
    Generic,
    Callable,
    Awaitable,
    AsyncContextManager
)
from functools import partial
from contextlib import asynccontextmanager as acm
from dataclasses import dataclass

import trio
import tractor

from msgspec.msgpack import (
    Encoder,
    Decoder
)

from tractor.ipc._ringbuf import (
    RBToken,
    PayloadT,
    RingBufferSendChannel,
    RingBufferReceiveChannel,
    attach_to_ringbuf_sender,
    attach_to_ringbuf_receiver
)

from tractor.trionics import (
    order_send_channel,
    order_receive_channel
)

import tractor.linux._fdshare as fdshare


log = tractor.log.get_logger(__name__)


ChannelType = TypeVar('ChannelType')


@dataclass
class ChannelInfo:
    token: RBToken
    channel: ChannelType
    cancel_scope: trio.CancelScope
    teardown: trio.Event


class ChannelManager(Generic[ChannelType]):
    '''
    Helper for managing channel resources and their handler tasks with
    cancellation, add or remove channels dynamically!

    '''

    def __init__(
        self,
        # nursery used to spawn channel handler tasks
        n: trio.Nursery,

        # acm will be used for setup & teardown of channel resources
        open_channel_acm: Callable[..., AsyncContextManager[ChannelType]],

        # long running bg task to handle channel
        channel_task: Callable[..., Awaitable[None]]
    ):
        self._n = n
        self._open_channel = open_channel_acm
        self._channel_task = channel_task

        # signal when a new channel conects and we previously had none
        self._connect_event = trio.Event()

        # store channel runtime variables
        self._channels: list[ChannelInfo] = []

        self._is_closed: bool = True

    @property
    def closed(self) -> bool:
        return self._is_closed

    @property
    def channels(self) -> list[ChannelInfo]:
        return self._channels

    async def _channel_handler_task(
        self,
        token: RBToken,
        task_status=trio.TASK_STATUS_IGNORED,
        **kwargs
    ):
        '''
        Open channel resources, add to internal data structures, signal channel
        connect through trio.Event, and run `channel_task` with cancel scope,
        and finally, maybe remove channel from internal data structures.

        Spawned by `add_channel` function, lock is held from begining of fn
        until `task_status.started()` call.

        kwargs are proxied to `self._open_channel` acm.
        '''
        async with self._open_channel(
            token,
            **kwargs
        ) as chan:
            cancel_scope = trio.CancelScope()
            info = ChannelInfo(
                token=token,
                channel=chan,
                cancel_scope=cancel_scope,
                teardown=trio.Event()
            )
            self._channels.append(info)

            if len(self) == 1:
                self._connect_event.set()

            task_status.started()

            with cancel_scope:
                await self._channel_task(info)

        self._maybe_destroy_channel(token.shm_name)

    def _find_channel(self, name: str) -> tuple[int, ChannelInfo] | None:
        '''
        Given a channel name maybe return its index and value from
        internal _channels list.

        Only use after acquiring lock.
        '''
        for entry in enumerate(self._channels):
            i, info = entry
            if info.token.shm_name == name:
                return entry

        return None


    def _maybe_destroy_channel(self, name: str):
        '''
        If channel exists cancel its scope and remove from internal
        _channels list.

        '''
        maybe_entry = self._find_channel(name)
        if maybe_entry:
            i, info = maybe_entry
            info.cancel_scope.cancel()
            info.teardown.set()
            del self._channels[i]

    async def add_channel(
        self,
        token: RBToken,
        **kwargs
    ):
        '''
        Add a new channel to be handled

        '''
        if self.closed:
            raise trio.ClosedResourceError

        await self._n.start(partial(
            self._channel_handler_task,
            RBToken.from_msg(token),
            **kwargs
        ))

    async def remove_channel(self, name: str):
        '''
        Remove a channel and stop its handling

        '''
        if self.closed:
            raise trio.ClosedResourceError

        maybe_entry = self._find_channel(name)
        if not maybe_entry:
            # return
            raise RuntimeError(
                f'tried to remove channel {name} but if does not exist'
            )

        i, info = maybe_entry
        self._maybe_destroy_channel(name)

        await info.teardown.wait()

        # if that was last channel reset connect event
        if len(self) == 0:
            self._connect_event = trio.Event()

    async def wait_for_channel(self):
        '''
        Wait until at least one channel added

        '''
        if self.closed:
            raise trio.ClosedResourceError

        await self._connect_event.wait()
        self._connect_event = trio.Event()

    def __len__(self) -> int:
        return len(self._channels)

    def __getitem__(self, name: str):
        maybe_entry = self._find_channel(name)
        if maybe_entry:
            _, info = maybe_entry
            return info

        raise KeyError(f'Channel {name} not found!')

    def open(self):
        self._is_closed = False

    async def close(self) -> None:
        if self.closed:
            log.warning('tried to close ChannelManager but its already closed...')
            return

        for info in self._channels:
            if info.channel.closed:
                continue

            await info.channel.aclose()
            await self.remove_channel(info.token.shm_name)

        self._is_closed = True


'''
Ring buffer publisher & subscribe pattern mediated by `ringd` actor.

'''


class RingBufferPublisher(trio.abc.SendChannel[PayloadT]):
    '''
    Use ChannelManager to create a multi ringbuf round robin sender that can
    dynamically add or remove more outputs.

    Don't instantiate directly, use `open_ringbuf_publisher` acm to manage its
    lifecycle.

    '''
    def __init__(
        self,
        n: trio.Nursery,

        # amount of msgs to each ring before switching turns
        msgs_per_turn: int = 1,

        # global batch size for all channels
        batch_size: int = 1,

        encoder: Encoder | None = None
    ):
        self._batch_size: int = batch_size
        self.msgs_per_turn = msgs_per_turn
        self._enc = encoder

        # helper to manage acms + long running tasks
        self._chanmngr = ChannelManager[RingBufferSendChannel[PayloadT]](
            n,
            self._open_channel,
            self._channel_task
        )

        # ensure no concurrent `.send()` calls
        self._send_lock = trio.StrictFIFOLock()

        # index of channel to be used for next send
        self._next_turn: int = 0
        # amount of messages sent this turn
        self._turn_msgs: int = 0
        # have we closed this publisher?
        # set to `False` on `.__aenter__()`
        self._is_closed: bool = True

    @property
    def closed(self) -> bool:
        return self._is_closed

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @batch_size.setter
    def batch_size(self, value: int) -> None:
        for info in self.channels:
            info.channel.batch_size = value

    @property
    def channels(self) -> list[ChannelInfo]:
        return self._chanmngr.channels

    def _get_next_turn(self) -> int:
        '''
        Maybe switch turn and reset self._turn_msgs or just increment it.
        Return current turn
        '''
        if self._turn_msgs == self.msgs_per_turn:
            self._turn_msgs = 0
            self._next_turn += 1

            if self._next_turn >= len(self.channels):
                self._next_turn = 0

        else:
            self._turn_msgs += 1

        return self._next_turn

    def get_channel(self, name: str) -> ChannelInfo:
        '''
        Get underlying ChannelInfo from name

        '''
        return self._chanmngr[name]

    async def add_channel(
        self,
        token: RBToken,
    ):
        await self._chanmngr.add_channel(token)

    async def remove_channel(self, name: str):
        await self._chanmngr.remove_channel(name)

    @acm
    async def _open_channel(

        self,
        token: RBToken

    ) -> AsyncContextManager[RingBufferSendChannel[PayloadT]]:
        async with attach_to_ringbuf_sender(
            token,
            batch_size=self._batch_size,
            encoder=self._enc
        ) as ring:
            yield ring

    async def _channel_task(self, info: ChannelInfo) -> None:
        '''
        Wait forever until channel cancellation

        '''
        await trio.sleep_forever()

    async def send(self, msg: bytes):
        '''
        If no output channels connected, wait until one, then fetch the next
        channel based on turn.

        Needs to acquire `self._send_lock` to ensure no concurrent calls.

        '''
        if self.closed:
            raise trio.ClosedResourceError

        if self._send_lock.locked():
            raise trio.BusyResourceError

        async with self._send_lock:
            # wait at least one decoder connected
            if len(self.channels) == 0:
                await self._chanmngr.wait_for_channel()

            turn = self._get_next_turn()

            info = self.channels[turn]
            await info.channel.send(msg)

    async def broadcast(self, msg: PayloadT):
        '''
        Send a msg to all channels, if no channels connected, does nothing.
        '''
        if self.closed:
            raise trio.ClosedResourceError

        for info in self.channels:
            await info.channel.send(msg)

    async def flush(self, new_batch_size: int | None = None):
        for info in self.channels:
            try:
                await info.channel.flush(new_batch_size=new_batch_size)

            except trio.ClosedResourceError:
                ...

    async def __aenter__(self):
        self._is_closed = False
        self._chanmngr.open()
        return self

    async def aclose(self) -> None:
        if self.closed:
            log.warning('tried to close RingBufferPublisher but its already closed...')
            return

        await self._chanmngr.close()

        self._is_closed = True


class RingBufferSubscriber(trio.abc.ReceiveChannel[PayloadT]):
    '''
    Use ChannelManager to create a multi ringbuf receiver that can
    dynamically add or remove more inputs and combine all into a single output.

    In order for `self.receive` messages to be returned in order, publisher
    will send all payloads as `OrderedPayload` msgpack encoded msgs, this
    allows our channel handler tasks to just stash the out of order payloads
    inside `self._pending_payloads` and if a in order payload is available
    signal through `self._new_payload_event`.

    On `self.receive` we wait until at least one channel is connected, then if
    an in order payload is pending, we pop and return it, in case no in order
    payload is available wait until next `self._new_payload_event.set()`.

    '''
    def __init__(
        self,
        n: trio.Nursery,

        decoder: Decoder | None = None
    ):
        self._dec = decoder
        self._chanmngr = ChannelManager[RingBufferReceiveChannel[PayloadT]](
            n,
            self._open_channel,
            self._channel_task
        )

        self._schan, self._rchan = trio.open_memory_channel(0)

        self._is_closed: bool = True

        self._receive_lock = trio.StrictFIFOLock()

    @property
    def closed(self) -> bool:
        return self._is_closed

    @property
    def channels(self) -> list[ChannelInfo]:
        return self._chanmngr.channels

    def get_channel(self, name: str):
        return self._chanmngr[name]

    async def add_channel(
        self,
        token: RBToken
    ):
        await self._chanmngr.add_channel(token)

    async def remove_channel(self, name: str):
        await self._chanmngr.remove_channel(name)

    @acm
    async def _open_channel(

        self,
        token: RBToken

    ) -> AsyncContextManager[RingBufferSendChannel]:
        async with attach_to_ringbuf_receiver(
            token,
            decoder=self._dec
        ) as ring:
            yield ring

    async def _channel_task(self, info: ChannelInfo) -> None:
        '''
        Iterate over receive channel messages, decode them as `OrderedPayload`s
        and stash them in `self._pending_payloads`, in case we can pop next in
        order payload, signal through setting `self._new_payload_event`.

        '''
        while True:
            try:
                msg = await info.channel.receive()
                await self._schan.send(msg)

            except tractor.linux.eventfd.EFDReadCancelled as e:
                # when channel gets removed while we are doing a receive
                log.exception(e)
                break

            except trio.EndOfChannel:
                break

            except trio.ClosedResourceError:
                break

    async def receive(self) -> PayloadT:
        '''
        Receive next in order msg
        '''
        if self.closed:
            raise trio.ClosedResourceError

        if self._receive_lock.locked():
            raise trio.BusyResourceError

        async with self._receive_lock:
            return await self._rchan.receive()

    async def __aenter__(self):
        self._is_closed = False
        self._chanmngr.open()
        return self

    async def aclose(self) -> None:
        if self.closed:
            return

        await self._chanmngr.close()
        await self._schan.aclose()
        await self._rchan.aclose()

        self._is_closed = True


'''
Actor module for managing publisher & subscriber channels remotely through
`tractor.context` rpc
'''

@dataclass
class PublisherEntry:
    publisher: RingBufferPublisher | None = None
    is_set: trio.Event = trio.Event()


_publishers: dict[str, PublisherEntry] = {}


def maybe_init_publisher(topic: str) -> PublisherEntry:
    entry = _publishers.get(topic, None)
    if not entry:
        entry = PublisherEntry()
        _publishers[topic] = entry

    return entry


def set_publisher(topic: str, pub: RingBufferPublisher):
    global _publishers

    entry = _publishers.get(topic, None)
    if not entry:
        entry = maybe_init_publisher(topic)

    if entry.publisher:
        raise RuntimeError(
            f'publisher for topic {topic} already set on {tractor.current_actor()}'
        )

    entry.publisher = pub
    entry.is_set.set()


def get_publisher(topic: str = 'default') -> RingBufferPublisher:
    entry = _publishers.get(topic, None)
    if not entry or not entry.publisher:
        raise RuntimeError(
            f'{tractor.current_actor()} tried to get publisher'
            'but it\'s not set'
        )

    return entry.publisher


async def wait_publisher(topic: str) -> RingBufferPublisher:
    entry = maybe_init_publisher(topic)
    await entry.is_set.wait()
    return entry.publisher


@tractor.context
async def _add_pub_channel(
    ctx: tractor.Context,
    topic: str,
    token: RBToken
):
    publisher = await wait_publisher(topic)
    await publisher.add_channel(token)


@tractor.context
async def _remove_pub_channel(
    ctx: tractor.Context,
    topic: str,
    ring_name: str
):
    publisher = await wait_publisher(topic)
    maybe_token = fdshare.maybe_get_fds(ring_name)
    if maybe_token:
        await publisher.remove_channel(ring_name)


@acm
async def open_pub_channel_at(
    actor_name: str,
    token: RBToken,
    topic: str = 'default',
):
    async with tractor.find_actor(actor_name) as portal:
        await portal.run(_add_pub_channel, topic=topic, token=token)
        try:
            yield

        except trio.Cancelled:
            log.warning(
                'open_pub_channel_at got cancelled!\n'
                f'\tactor_name = {actor_name}\n'
                f'\ttoken = {token}\n'
            )
            raise

        await portal.run(_remove_pub_channel, topic=topic, ring_name=token.shm_name)


@dataclass
class SubscriberEntry:
    subscriber: RingBufferSubscriber | None = None
    is_set: trio.Event = trio.Event()


_subscribers: dict[str, SubscriberEntry] = {}


def maybe_init_subscriber(topic: str) -> SubscriberEntry:
    entry = _subscribers.get(topic, None)
    if not entry:
        entry = SubscriberEntry()
        _subscribers[topic] = entry

    return entry


def set_subscriber(topic: str, sub: RingBufferSubscriber):
    global _subscribers

    entry = _subscribers.get(topic, None)
    if not entry:
        entry = maybe_init_subscriber(topic)

    if entry.subscriber:
        raise RuntimeError(
            f'subscriber for topic {topic} already set on {tractor.current_actor()}'
        )

    entry.subscriber = sub
    entry.is_set.set()


def get_subscriber(topic: str = 'default') -> RingBufferSubscriber:
    entry = _subscribers.get(topic, None)
    if not entry or not entry.subscriber:
        raise RuntimeError(
            f'{tractor.current_actor()} tried to get subscriber'
            'but it\'s not set'
        )

    return entry.subscriber


async def wait_subscriber(topic: str) -> RingBufferSubscriber:
    entry = maybe_init_subscriber(topic)
    await entry.is_set.wait()
    return entry.subscriber


@tractor.context
async def _add_sub_channel(
    ctx: tractor.Context,
    topic: str,
    token: RBToken
):
    subscriber = await wait_subscriber(topic)
    await subscriber.add_channel(token)


@tractor.context
async def _remove_sub_channel(
    ctx: tractor.Context,
    topic: str,
    ring_name: str
):
    subscriber = await wait_subscriber(topic)
    maybe_token = fdshare.maybe_get_fds(ring_name)
    if maybe_token:
        await subscriber.remove_channel(ring_name)


@acm
async def open_sub_channel_at(
    actor_name: str,
    token: RBToken,
    topic: str = 'default',
):
    async with tractor.find_actor(actor_name) as portal:
        await portal.run(_add_sub_channel, topic=topic, token=token)
        try:
            yield

        except trio.Cancelled:
            log.warning(
                'open_sub_channel_at got cancelled!\n'
                f'\tactor_name = {actor_name}\n'
                f'\ttoken = {token}\n'
            )
            raise

        await portal.run(_remove_sub_channel, topic=topic, ring_name=token.shm_name)


'''
High level helpers to open publisher & subscriber
'''


@acm
async def open_ringbuf_publisher(
    # name to distinguish this publisher
    topic: str = 'default',

    # global batch size for channels
    batch_size: int = 1,

    # messages before changing output channel
    msgs_per_turn: int = 1,

    encoder: Encoder | None = None,

    # ensure subscriber receives in same order publisher sent
    # causes it to use wrapped payloads which contain the og
    # index
    guarantee_order: bool = False,

    # on creation, set the `_publisher` global in order to use the provided
    # tractor.context & helper utils for adding and removing new channels from
    # remote actors
    set_module_var: bool = True

) -> AsyncContextManager[RingBufferPublisher]:
    '''
    Open a new ringbuf publisher

    '''
    async with (
        trio.open_nursery(strict_exception_groups=False) as n,
        RingBufferPublisher(
            n,
            batch_size=batch_size,
            encoder=encoder,
        ) as publisher
    ):
        if guarantee_order:
            order_send_channel(publisher)

        if set_module_var:
            set_publisher(topic, publisher)

        yield publisher

        n.cancel_scope.cancel()


@acm
async def open_ringbuf_subscriber(
    # name to distinguish this subscriber
    topic: str = 'default',

    decoder: Decoder | None = None,

    # expect indexed payloads and unwrap them in order
    guarantee_order: bool = False,

    # on creation, set the `_subscriber` global in order to use the provided
    # tractor.context & helper utils for adding and removing new channels from
    # remote actors
    set_module_var: bool = True
) -> AsyncContextManager[RingBufferPublisher]:
    '''
    Open a new ringbuf subscriber

    '''
    async with (
        trio.open_nursery(strict_exception_groups=False) as n,
        RingBufferSubscriber(n, decoder=decoder) as subscriber
    ):
        # maybe monkey patch `.receive` to use indexed payloads
        if guarantee_order:
            order_receive_channel(subscriber)

        # maybe set global module var for remote actor channel updates
        if set_module_var:
            set_subscriber(topic, subscriber)

        yield subscriber

        n.cancel_scope.cancel()
