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
IPC-compat cross-mem-boundary object pointer.

'''

# TODO: integration with our ``enable_modules: list[str]`` caps sys.

# ``pkgutil.resolve_name()`` internally uses
# ``importlib.import_module()`` which can be filtered by inserting
# a ``MetaPathFinder`` into ``sys.meta_path`` (which we could do before
# entering the ``_runtime.process_messages()`` loop).
# - https://github.com/python/cpython/blob/main/Lib/pkgutil.py#L645
# - https://stackoverflow.com/questions/1350466/preventing-python-code-from-importing-certain-modules
#   - https://stackoverflow.com/a/63320902
#   - https://docs.python.org/3/library/sys.html#sys.meta_path

# the new "Implicit Namespace Packages" might be relevant?
# - https://www.python.org/dev/peps/pep-0420/

# add implicit serialized message type support so that paths can be
# handed directly to IPC primitives such as streams and `Portal.run()`
# calls:
# - via ``msgspec``:
#   - https://jcristharif.com/msgspec/api.html#struct
#   - https://jcristharif.com/msgspec/extending.html
# via ``msgpack-python``:
# - https://github.com/msgpack/msgpack-python#packingunpacking-of-custom-data-type

from __future__ import annotations
from inspect import (
    isfunction,
    ismethod,
)
from pkgutil import resolve_name


class NamespacePath(str):
    '''
    A serializeable `str`-subtype implementing a "namespace
    pointer" to any Python object reference (like a function)
    using the same format as the built-in `pkgutil.resolve_name()`
    system.

    A value describes a target's module-path and namespace-key
    separated by a ':' and thus can be easily used as
    a IPC-message-native reference-type allowing memory isolated
    actors to point-and-load objects via a minimal `str` value.

    '''
    _ref: object | type | None = None

    # TODO: support providing the ns instance in
    # order to support 'self.<meth>` style to make
    # `Portal.run_from_ns()` work!
    # _ns: ModuleType|type|None = None

    def load_ref(self) -> object | type:
        if self._ref is None:
            self._ref = resolve_name(self)
        return self._ref

    @staticmethod
    def _mk_fqnp(
        ref: type|object,
    ) -> tuple[str, str]:
        '''
        Generate a minial `str` pair which describes a python
        object's namespace path and object/type name.

        In more precise terms something like:
          - 'py.namespace.path:object_name',
          - eg.'tractor.msg:NamespacePath' will be the ``str`` form
            of THIS type XD

        '''
        if isfunction(ref):
            name: str = getattr(ref, '__name__')
            mod_name: str = ref.__module__

        elif ismethod(ref):
            # build out the path manually i guess..?
            # TODO: better way?
            name: str = '.'.join([
                type(ref.__self__).__name__,
                ref.__func__.__name__,
            ])
            mod_name: str = ref.__self__.__module__

        else:  # object or other?
            # isinstance(ref, object)
            # and not isfunction(ref)
            name: str = type(ref).__name__
            mod_name: str = ref.__module__

        # TODO: return static value direactly?
        #
        # fully qualified namespace path, tuple.
        fqnp: tuple[str, str] = (
            mod_name,
            name,
        )
        return fqnp

    @classmethod
    def from_ref(
        cls,
        ref: type|object,

    ) -> NamespacePath:

        fqnp: tuple[str, str] = cls._mk_fqnp(ref)
        return cls(':'.join(fqnp))

    def to_tuple(
        self,

        # TODO: could this work re `self:<meth>` case from above?
        # load_ref: bool = True,

    ) -> tuple[str, str]:
        return self._mk_fqnp(
            self.load_ref()
        )
