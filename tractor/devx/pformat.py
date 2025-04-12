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
Pretty formatters for use throughout our internals.

Handy for logging and exception message content but also for `repr()`
in REPL(s).

'''
import sys
import textwrap
import traceback

from trio import CancelScope


def add_div(
    message: str,
    div_str: str = '------ - ------',

) -> str:
    '''
    Add a "divider string" to the input `message` with
    a little math to center it underneath.

    '''
    div_offset: int = (
        round(len(message)/2)+1
        -
        round(len(div_str)/2)+1
    )
    div_str: str = (
        '\n' + ' '*div_offset + f'{div_str}\n'
    )
    return div_str


def pformat_boxed_tb(
    tb_str: str,
    fields_str: str|None = None,
    field_prefix: str = ' |_',

    tb_box_indent: int|None = None,
    tb_body_indent: int = 1,
    boxer_header: str = '-'

) -> str:
    '''
    Create a "boxed" looking traceback string.

    Useful for emphasizing traceback text content as being an
    embedded attribute of some other object (like
    a `RemoteActorError` or other boxing remote error shuttle
    container).

    Any other parent/container "fields" can be passed in the
    `fields_str` input along with other prefix/indent settings.

    '''
    if (
        fields_str
        and
        field_prefix
    ):
        fields: str = textwrap.indent(
            fields_str,
            prefix=field_prefix,
        )
    else:
        fields = fields_str or ''

    tb_body = tb_str
    if tb_body_indent:
        tb_body: str = textwrap.indent(
            tb_str,
            prefix=tb_body_indent * ' ',
        )

    tb_box: str = (
        f'|\n'
        f' ------ {boxer_header} ------\n'
        f'{tb_body}'
        f' ------ {boxer_header}- ------\n'
        f'_|'
    )
    tb_box_indent: str = (
        tb_box_indent
        or
        1

        # (len(field_prefix))
        # ? ^-TODO-^ ? if you wanted another indent level
    )
    if tb_box_indent > 0:
        tb_box: str = textwrap.indent(
            tb_box,
            prefix=tb_box_indent * ' ',
        )

    return (
        fields
        +
        tb_box
    )


def pformat_exc(
    exc: Exception,
    header: str = '',
    message: str = '',
    body: str = '',
    with_type_header: bool = True,
) -> str:

    # XXX when the currently raised exception is this instance,
    # we do not ever use the "type header" style repr.
    is_being_raised: bool = False
    if (
        (curr_exc := sys.exception())
        and
        curr_exc is exc
    ):
        is_being_raised: bool = True

    with_type_header: bool = (
        with_type_header
        and
        not is_being_raised
    )

    # <RemoteActorError( .. )> style
    if (
        with_type_header
        and
        not header
    ):
        header: str = f'<{type(exc).__name__}('

    message: str = (
        message
        or
        exc.message
    )
    if message:
        # split off the first line so, if needed, it isn't
        # indented the same like the "boxed content" which
        # since there is no `.tb_str` is just the `.message`.
        lines: list[str] = message.splitlines()
        first: str = lines[0]
        message: str = message.removeprefix(first)

        # with a type-style header we,
        # - have no special message "first line" extraction/handling
        # - place the message a space in from the header:
        #  `MsgTypeError( <message> ..`
        #                 ^-here
        # - indent the `.message` inside the type body.
        if with_type_header:
            first = f' {first} )>'

        message: str = textwrap.indent(
            message,
            prefix=' '*2,
        )
        message: str = first + message

    tail: str = ''
    if (
        with_type_header
        and
        not message
    ):
        tail: str = '>'

    return (
        header
        +
        message
        +
        f'{body}'
        +
        tail
    )


def pformat_caller_frame(
    stack_limit: int = 1,
    box_tb: bool = True,
) -> str:
    '''
    Capture and return the traceback text content from
    `stack_limit` call frames up.

    '''
    tb_str: str = (
        '\n'.join(
            traceback.format_stack(limit=stack_limit)
        )
    )
    if box_tb:
        tb_str: str = pformat_boxed_tb(
            tb_str=tb_str,
            field_prefix='  ',
            indent='',
        )
    return tb_str


def pformat_cs(
    cs: CancelScope,
    var_name: str = 'cs',
    field_prefix: str = ' |_',
) -> str:
    '''
    Pretty format info about a `trio.CancelScope` including most of
    its public state and `._cancel_status`.

    The output can be modified to show a "var name" for the
    instance as a field prefix, just a simple str before each
    line more or less.

    '''

    fields: str = textwrap.indent(
        (
            f'cancel_called = {cs.cancel_called}\n'
            f'cancelled_caught = {cs.cancelled_caught}\n'
            f'_cancel_status = {cs._cancel_status}\n'
            f'shield = {cs.shield}\n'
        ),
        prefix=field_prefix,
    )
    return (
        f'{var_name}: {cs}\n'
        +
        fields
    )


def nest_from_op(
    input_op: str,  # TODO, Literal of all op-"symbols" from below?
    text: str,
    prefix_op: bool = True,  # unset is to suffix the first line
    # optionally suffix `text`, by def on a newline
    op_suffix='\n',

    nest_prefix: str = '|_',
    nest_indent: int|None = None,
    # XXX indent `next_prefix` "to-the-right-of" `input_op`
    # by this count of whitespaces (' ').
    rm_from_first_ln: str|None = None,

) -> str:
    '''
    Depth-increment the input (presumably hierarchy/supervision)
    input "tree string" below the provided `input_op` execution
    operator, so injecting a `"\n|_{input_op}\n"`and indenting the
    `tree_str` to nest content aligned with the ops last char.

    '''
    # `sclang` "structurred-concurrency-language": an ascii-encoded
    # symbolic alphabet to describe concurrent systems.
    #
    # ?TODO? aa more fomal idea for a syntax to the state of
    # concurrent systems as a "3-domain" (execution, scope, storage)
    # model and using a minimal ascii/utf-8 operator-set.
    #
    # try not to take any of this seriously yet XD
    #
    # > is a "play operator" indicating (CPU bound)
    #   exec/work/ops required at the "lowest level computing"
    #
    # execution primititves (tasks, threads, actors..) denote their
    # lifetime with '(' and ')' since parentheses normally are used
    # in many langs to denote function calls.
    #
    # starting = (
    # >(  opening/starting; beginning of the thread-of-exec (toe?)
    # (>  opened/started,  (finished spawning toe)
    # |_<Task: blah blah..>  repr of toe, in py these look like <objs>
    #
    # >) closing/exiting/stopping,
    # )> closed/exited/stopped,
    # |_<Task: blah blah..>
    #   [OR <), )< ?? ]
    #
    # ending = )
    # >c) cancelling to close/exit
    # c)> cancelled (caused close), OR?
    #  |_<Actor: ..>
    #   OR maybe "<c)" which better indicates the cancel being
    #   "delivered/returned" / returned" to LHS?
    #
    # >x)  erroring to eventuall exit
    # x)>  errored and terminated
    #  |_<Actor: ...>
    #
    # scopes: supers/nurseries, IPC-ctxs, sessions, perms, etc.
    # >{  opening
    # {>  opened
    # }>  closed
    # >}  closing
    #
    # storage: like queues, shm-buffers, files, etc..
    # >[  opening
    # [>  opened
    #  |_<FileObj: ..>
    #
    # >]  closing
    # ]>  closed

    # IPC ops: channels, transports, msging
    # =>  req msg
    # <=  resp msg
    # <=> 2-way streaming (of msgs)
    # <-  recv 1 msg
    # ->  send 1 msg
    #
    # TODO: still not sure on R/L-HS approach..?
    # =>(  send-req to exec start (task, actor, thread..)
    # (<=  recv-req to ^
    #
    # (<=  recv-req ^
    # <=(  recv-resp opened remote exec primitive
    # <=)  recv-resp closed
    #
    # )<=c req to stop due to cancel
    # c=>) req to stop due to cancel
    #
    # =>{  recv-req to open
    # <={  send-status that it closed
    #
    if (
        nest_prefix
        and
        nest_indent != 0
    ):
        if nest_indent is not None:
            nest_prefix: str = textwrap.indent(
                nest_prefix,
                prefix=nest_indent*' ',
            )
        nest_indent: int = len(nest_prefix)

    # determine body-text indent either by,
    # - using wtv explicit indent value is provided,
    # OR
    # - auto-calcing the indent to embed `text` under
    #   the `nest_prefix` if provided, **IFF** `nest_indent=None`.
    tree_str_indent: int = 0
    if nest_indent not in {0, None}:
        tree_str_indent = nest_indent
    elif (
        nest_prefix
        and
        nest_indent != 0
    ):
        tree_str_indent = len(nest_prefix)

    indented_tree_str: str = text
    if tree_str_indent:
        indented_tree_str: str = textwrap.indent(
            text,
            prefix=' '*tree_str_indent,
        )

    # inject any provided nesting-prefix chars
    # into the head of the first line.
    if nest_prefix:
        indented_tree_str: str = (
            f'{nest_prefix}{indented_tree_str[tree_str_indent:]}'
        )

    if (
        not prefix_op
        or
        rm_from_first_ln
    ):
        tree_lns: list[str] = indented_tree_str.splitlines()
        first: str = tree_lns[0]
        if rm_from_first_ln:
            first = first.strip().replace(
                rm_from_first_ln,
                '',
            )
        indented_tree_str: str = '\n'.join(tree_lns[1:])

        if prefix_op:
            indented_tree_str = (
                f'{first}\n'
                f'{indented_tree_str}'
            )

    if prefix_op:
        return (
            f'{input_op}{op_suffix}'
            f'{indented_tree_str}'
        )
    else:
        return (
            f'{first}{input_op}{op_suffix}'
            f'{indented_tree_str}'
        )
