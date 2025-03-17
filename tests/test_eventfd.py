import trio
import pytest
from tractor.ipc import (
    open_eventfd,
    EFDReadCancelled,
    EventFD
)


def test_eventfd_read_cancellation():
    '''
    Ensure EventFD.read raises EFDReadCancelled if EventFD.close()
    is called.

    '''
    fd = open_eventfd()

    async def _read(event: EventFD):
        with pytest.raises(EFDReadCancelled):
            await event.read()

    async def main():
        async with trio.open_nursery() as n:
            with (
                EventFD(fd, 'w') as event,
                trio.fail_after(3)
            ):
                n.start_soon(_read, event)
                await trio.sleep(0.2)
                event.close()

    trio.run(main)
