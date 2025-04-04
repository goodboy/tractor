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
A modular IPC layer supporting the power of cross-process SC!

'''
import platform

from ._chan import (
    _connect_chan as _connect_chan,
    Channel as Channel
)

if platform.system() == 'Linux':
    from ._ringbuf import (
        RBToken as RBToken,

        open_ringbuf as open_ringbuf,
        open_ringbuf_pair as open_ringbuf_pair,

        RingBufferSendChannel as RingBufferSendChannel,
        attach_to_ringbuf_sender as attach_to_ringbuf_sender,

        RingBufferReceiveChannel as RingBufferReceiveChannel,
        attach_to_ringbuf_receiver as attach_to_ringbuf_receiver,

        RingBufferChannel as RingBufferChannel,
        attach_to_ringbuf_channel as attach_to_ringbuf_channel,
    )
