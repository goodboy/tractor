import trio
import pytest
from tractor.linux.eventfd import (
    open_eventfd,
    EFDReadCancelled,
    EventFD
)


def test_read_cancellation():
    '''
    Ensure EventFD.read raises EFDReadCancelled if EventFD.close()
    is called.

    '''
    fd = open_eventfd()

    async def bg_read(event: EventFD):
        with pytest.raises(EFDReadCancelled):
            await event.read()

    async def main():
        async with trio.open_nursery() as n:
            with (
                EventFD(fd, 'w') as event,
                trio.fail_after(3)
            ):
                n.start_soon(bg_read, event)
                await trio.sleep(0.2)
                event.close()

    trio.run(main)


def test_read_trio_semantics():
    '''
    Ensure EventFD.read raises trio.ClosedResourceError and
    trio.BusyResourceError.

    '''

    fd = open_eventfd()

    async def bg_read(event: EventFD):
        try:
            await event.read()

        except EFDReadCancelled:
            ...

    async def main():
        async with trio.open_nursery() as n:

            # start background read and attempt
            # foreground read, should be busy
            with EventFD(fd, 'w') as event:
                n.start_soon(bg_read, event)
                await trio.sleep(0.2)
                with pytest.raises(trio.BusyResourceError):
                    await event.read()

            # attempt read after close
            with pytest.raises(trio.ClosedResourceError):
                await event.read()

    trio.run(main)
