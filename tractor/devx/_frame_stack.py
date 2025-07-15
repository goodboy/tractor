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
from contextlib import (
    _GeneratorContextManager,
    _AsyncGeneratorContextManager,
)
from functools import partial
import inspect
import textwrap
from types import (
    FrameType,
    FunctionType,
    MethodType,
    CodeType,
)
from typing import (
    Any,
    Callable,
    Type,
)

import pdbp
from tractor.log import get_logger
import trio
from tractor.msg import (
    pretty_struct,
    NamespacePath,
)
import wrapt


log = get_logger(__name__)

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


def get_ns_and_func_from_frame(
    frame: FrameType,
) -> Callable:
    '''
    Return the corresponding function object reference from
    a `FrameType`, and return it and it's parent namespace `dict`.

    '''
    ns: dict[str, Any]

    # for a method, go up a frame and lookup the name in locals()
    if '.' in (qualname := frame.f_code.co_qualname):
        cls_name, _, func_name = qualname.partition('.')
        ns = frame.f_back.f_locals[cls_name].__dict__

    else:
        func_name: str = frame.f_code.co_name
        ns = frame.f_globals

    return (
        ns,
        ns[func_name],
    )


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


class CallerInfo(pretty_struct.Struct):
    # https://docs.python.org/dev/reference/datamodel.html#frame-objects
    # https://docs.python.org/dev/library/inspect.html#the-interpreter-stack
    _api_frame: FrameType

    @property
    def api_frame(self) -> FrameType:
        try:
            self._api_frame.clear()
        except RuntimeError:
            # log.warning(
            print(
                f'Frame {self._api_frame} for {self.api_func} is still active!'
            )

        return self._api_frame

    _api_func: Callable

    @property
    def api_func(self) -> Callable:
        return self._api_func

    _caller_frames_up: int|None = 1
    _caller_frame: FrameType|None = None  # cached after first stack scan

    @property
    def api_nsp(self) -> NamespacePath|None:
        func: FunctionType = self.api_func
        if func:
            return NamespacePath.from_ref(func)

        return '<unknown>'

    @property
    def caller_frame(self) -> FrameType:

        # if not already cached, scan up stack explicitly by
        # configured count.
        if not self._caller_frame:
            if self._caller_frames_up:
                for _ in range(self._caller_frames_up):
                    caller_frame: FrameType|None = self.api_frame.f_back

                if not caller_frame:
                    raise ValueError(
                        'No frame exists {self._caller_frames_up} up from\n'
                        f'{self.api_frame} @ {self.api_nsp}\n'
                    )

            self._caller_frame = caller_frame

        return self._caller_frame

    @property
    def caller_nsp(self) -> NamespacePath|None:
        func: FunctionType = self.api_func
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
                _api_frame=rt_frame,
                _api_func=func_ref_from_frame(rt_frame),
                _caller_frames_up=go_up_iframes,
            )

    return None


_frame2callerinfo_cache: dict[FrameType, CallerInfo] = {}


# TODO: -[x] move all this into new `.devx._frame_stack`!
# -[ ] consider rename to _callstack?
# -[ ] prolly create a `@runtime_api` dec?
#   |_ @api_frame seems better?
# -[ ] ^- make it capture and/or accept buncha optional
#     meta-data like a fancier version of `@pdbp.hideframe`.
#
def api_frame(
    wrapped: Callable|None = None,
    *,
    caller_frames_up: int = 1,

) -> Callable:

    # handle the decorator called WITHOUT () case,
    # i.e. just @api_frame, NOT @api_frame(extra=<blah>)
    if wrapped is None:
        return partial(
            api_frame,
            caller_frames_up=caller_frames_up,
        )

    @wrapt.decorator
    async def wrapper(
        wrapped: Callable,
        instance: object,
        args: tuple,
        kwargs: dict,
    ):
        # maybe cache the API frame for this call
        global _frame2callerinfo_cache
        this_frame: FrameType = inspect.currentframe()
        api_frame: FrameType = this_frame.f_back

        if not _frame2callerinfo_cache.get(api_frame):
            _frame2callerinfo_cache[api_frame] = CallerInfo(
                _api_frame=api_frame,
                _api_func=wrapped,
                _caller_frames_up=caller_frames_up,
            )

        return wrapped(*args, **kwargs)

    # annotate the function as a "api function", meaning it is
    # a function for which the function above it in the call stack should be
    # non-`tractor` code aka "user code".
    #
    # in the global frame cache for easy lookup from a given
    # func-instance
    wrapped._call_infos: dict[FrameType, CallerInfo] = _frame2callerinfo_cache
    wrapped.__api_func__: bool = True
    return wrapper(wrapped)


# TODO: something like this instead of the adhoc frame-unhiding
# blocks all over the runtime!! XD
# -[ ] ideally we can expect a certain error (set) and if something
#     else is raised then all frames below the wrapped one will be
#     un-hidden via `__tracebackhide__: bool = False`.
# |_ might need to dynamically mutate the code objs like
#    `pdbp.hideframe()` does?
# -[ ] use this as a `@acm` decorator as introed in 3.10?
# @acm
# async def unhide_frame_when_not(
#     error_set: set[BaseException],
# ) -> TracebackType:
#     ...


def hide_runtime_frames() -> dict[FunctionType, CodeType]:
    '''
    Hide call-stack frames for various std-lib and `trio`-API primitives
    such that the tracebacks presented from our runtime are as minimized
    as possible, particularly from inside a `PdbREPL`.

    '''
    # XXX HACKZONE XXX
    #  hide exit stack frames on nurseries and cancel-scopes!
    # |_ so avoid seeing it when the `pdbp` REPL is first engaged from
    #    inside a `trio.open_nursery()` scope (with no line after it
    #    in before the block end??).
    #
    # TODO: FINALLY got this workin originally with
    #  `@pdbp.hideframe` around the `wrapper()` def embedded inside
    #  `_ki_protection_decoratior()`.. which is in the module:
    #  /home/goodboy/.virtualenvs/tractor311/lib/python3.11/site-packages/trio/_core/_ki.py
    #
    # -[ ] make an issue and patch for `trio` core? maybe linked
    #    to the long outstanding `pdb` one below?
    #   |_ it's funny that there's frame hiding throughout `._run.py`
    #      but not where it matters on the below exit funcs..
    #
    # -[ ] provide a patchset for the lonstanding
    #   |_ https://github.com/python-trio/trio/issues/1155
    #
    # -[ ] make a linked issue to ^ and propose allowing all the
    #     `._core._run` code to have their `__tracebackhide__` value
    #     configurable by a `RunVar` to allow getting scheduler frames
    #     if desired through configuration?
    #
    # -[ ] maybe dig into the core `pdb` issue why the extra frame is shown
    #      at all?
    #
    funcs: list[FunctionType] = [
        trio._core._run.NurseryManager.__aexit__,
        trio._core._run.CancelScope.__exit__,
         _GeneratorContextManager.__exit__,
         _AsyncGeneratorContextManager.__aexit__,
         _AsyncGeneratorContextManager.__aenter__,
         trio.Event.wait,
    ]
    func_list_str: str = textwrap.indent(
        "\n".join(f.__qualname__ for f in funcs),
        prefix=' |_ ',
    )
    log.devx(
        'Hiding the following runtime frames by default:\n'
        f'{func_list_str}\n'
    )

    codes: dict[FunctionType, CodeType] = {}
    for ref in funcs:
        # stash a pre-modified version of each ref's code-obj
        # so it can be reverted later if needed.
        codes[ref] = ref.__code__
        pdbp.hideframe(ref)
    #
    # pdbp.hideframe(trio._core._run.NurseryManager.__aexit__)
    # pdbp.hideframe(trio._core._run.CancelScope.__exit__)
    # pdbp.hideframe(_GeneratorContextManager.__exit__)
    # pdbp.hideframe(_AsyncGeneratorContextManager.__aexit__)
    # pdbp.hideframe(_AsyncGeneratorContextManager.__aenter__)
    # pdbp.hideframe(trio.Event.wait)
    return codes
