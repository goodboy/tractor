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
Pretty formatters for use throughout the code base.
Mostly handy for logging and exception message content.

'''
import textwrap
import traceback


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
        f' ------ - ------\n'
        f'{tb_body}'
        f' ------ - ------\n'
        f'_|\n'
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
