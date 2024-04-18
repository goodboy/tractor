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
Tools for code-object annotation, introspection and mutation
as it pertains to improving the grok-ability of our runtime!

'''
from __future__ import annotations
import inspect
# import msgspec
# from pprint import pformat
from types import (
    FrameType,
    FunctionType,
    MethodType,
    # CodeType,
)
from typing import (
    # Any,
    Callable,
    # TYPE_CHECKING,
    Type,
)

from tractor.msg import (
    pretty_struct,
    NamespacePath,
)


# TODO: yeah, i don't love this and we should prolly just
# write a decorator that actually keeps a stupid ref to the func
# obj..
def get_class_from_frame(fr: FrameType) -> (
    FunctionType
    |MethodType
):
    '''
    Attempt to get the function (or method) reference
    from a given `FrameType`.

    Verbatim from an SO:
    https://stackoverflow.com/a/2220759

    '''
    args, _, _, value_dict = inspect.getargvalues(fr)

    # we check the first parameter for the frame function is
    # named 'self'
    if (
        len(args)
        and
        # TODO: other cases for `@classmethod` etc..?)
        args[0] == 'self'
    ):
        # in that case, 'self' will be referenced in value_dict
        instance: object = value_dict.get('self')
        if instance:
          # return its class
          return getattr(
              instance,
              '__class__',
              None,
          )

    # return None otherwise
    return None


def func_ref_from_frame(
    frame: FrameType,
) -> Callable:
    func_name: str = frame.f_code.co_name
    try:
        return frame.f_globals[func_name]
    except KeyError:
        cls: Type|None = get_class_from_frame(frame)
        if cls:
            return getattr(
                cls,
                func_name,
            )


# TODO: move all this into new `.devx._code`!
# -[ ] prolly create a `@runtime_api` dec?
# -[ ] ^- make it capture and/or accept buncha optional
#     meta-data like a fancier version of `@pdbp.hideframe`.
#
class CallerInfo(pretty_struct.Struct):
    rt_fi: inspect.FrameInfo
    call_frame: FrameType

    @property
    def api_func_ref(self) -> Callable|None:
        return func_ref_from_frame(self.rt_fi.frame)

    @property
    def api_nsp(self) -> NamespacePath|None:
        func: FunctionType = self.api_func_ref
        if func:
            return NamespacePath.from_ref(func)

        return '<unknown>'

    @property
    def caller_func_ref(self) -> Callable|None:
        return func_ref_from_frame(self.call_frame)

    @property
    def caller_nsp(self) -> NamespacePath|None:
        func: FunctionType = self.caller_func_ref
        if func:
            return NamespacePath.from_ref(func)

        return '<unknown>'


def find_caller_info(
    dunder_var: str = '__runtimeframe__',
    iframes:int = 1,
    check_frame_depth: bool = True,

) -> CallerInfo|None:
    '''
    Scan up the callstack for a frame with a `dunder_var: str` variable
    and return the `iframes` frames above it.

    By default we scan for a `__runtimeframe__` scope var which
    denotes a `tractor` API above which (one frame up) is "user
    app code" which "called into" the `tractor` method or func.

    TODO: ex with `Portal.open_context()`

    '''
    # TODO: use this instead?
    # https://docs.python.org/3/library/inspect.html#inspect.getouterframes
    frames: list[inspect.FrameInfo] = inspect.stack()
    for fi in frames:
        assert (
            fi.function
            ==
            fi.frame.f_code.co_name
        )
        this_frame: FrameType = fi.frame
        dunder_val: int|None = this_frame.f_locals.get(dunder_var)
        if dunder_val:
            go_up_iframes: int = (
                dunder_val  # could be 0 or `True` i guess?
                or
                iframes
            )
            rt_frame: FrameType = fi.frame
            call_frame = rt_frame
            for i in range(go_up_iframes):
                call_frame = call_frame.f_back

            return CallerInfo(
                rt_fi=fi,
                call_frame=call_frame,
            )

    return None
