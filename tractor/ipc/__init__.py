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
