import platform

from ._chan import (
    _connect_chan as _connect_chan,
    MsgTransport as MsgTransport,
    Channel as Channel
)

if platform.system() == 'Linux':
    from ._linux import (
        EFD_SEMAPHORE as EFD_SEMAPHORE,
        EFD_CLOEXEC as EFD_CLOEXEC,
        EFD_NONBLOCK as EFD_NONBLOCK,
        open_eventfd as open_eventfd,
        write_eventfd as write_eventfd,
        read_eventfd as read_eventfd,
        close_eventfd as close_eventfd,
        EventFD as EventFD,
    )

    from ._ringbuf import (
        RingBuffSender as RingBuffSender,
        RingBuffReceiver as RingBuffReceiver
    )
