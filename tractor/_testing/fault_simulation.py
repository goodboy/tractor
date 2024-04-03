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
`pytest` utils helpers and plugins for testing `tractor`'s runtime
and applications.

'''

from tractor import (
    MsgStream,
)

async def break_ipc(
    stream: MsgStream,
    method: str|None = None,
    pre_close: bool = False,

    def_method: str = 'socket_close',

) -> None:
    '''
    XXX: close the channel right after an error is raised
    purposely breaking the IPC transport to make sure the parent
    doesn't get stuck in debug or hang on the connection join.
    this more or less simulates an infinite msg-receive hang on
    the other end.

    '''
    # close channel via IPC prot msging before
    # any transport breakage
    if pre_close:
        await stream.aclose()

    method: str = method or def_method
    print(
        '#################################\n'
        'Simulating CHILD-side IPC BREAK!\n'
        f'method: {method}\n'
        f'pre `.aclose()`: {pre_close}\n'
        '#################################\n'
    )

    match method:
        case 'socket_close':
            await stream._ctx.chan.transport.stream.aclose()

        case 'socket_eof':
            # NOTE: `trio` does the following underneath this
            # call in `src/trio/_highlevel_socket.py`:
            # `Stream.socket.shutdown(tsocket.SHUT_WR)`
            await stream._ctx.chan.transport.stream.send_eof()

        # TODO: remove since now this will be invalid with our
        # new typed msg spec?
        # case 'msg':
        #     await stream._ctx.chan.send(None)

        # TODO: the actual real-world simulated cases like
        # transport layer hangs and/or lower layer 2-gens type
        # scenarios..
        #
        # -[ ] already have some issues for this general testing
        # area:
        #  - https://github.com/goodboy/tractor/issues/97
        #  - https://github.com/goodboy/tractor/issues/124
        #   - PR from @guille:
        #     https://github.com/goodboy/tractor/pull/149
        # case 'hang':
        # TODO: framework research:
        #
        # - https://github.com/GuoTengda1993/pynetem
        # - https://github.com/shopify/toxiproxy
        # - https://manpages.ubuntu.com/manpages/trusty/man1/wirefilter.1.html

        case _:
            raise RuntimeError(
                f'IPC break method unsupported: {method}'
            )
