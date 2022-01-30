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
Built-in messaging patterns, types, APIs and helpers.

'''

# TODO: integration with our ``enable_modules: list[str]`` caps sys.

# ``pkgutil.resolve_name()`` internally uses
# ``importlib.import_module()`` which can be filtered by inserting
# a ``MetaPathFinder`` into ``sys.meta_path`` (which we could do before
# entering the ``Actor._process_messages()`` loop).
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
from pkgutil import resolve_name


class NamespacePath(str):
    '''
    A serializeable description of a (function) Python object location
    described by the target's module path and namespace key meant as
    a message-native "packet" to allows actors to point-and-load objects
    by absolute reference.

    '''
    _ref: object = None

    def load_ref(self) -> object:
        if self._ref is None:
            self._ref = resolve_name(self)
        return self._ref

    def to_tuple(
        self,

    ) -> tuple[str, str]:
        ref = self.load_ref()
        return ref.__module__, getattr(ref, '__name__', '')

    @classmethod
    def from_ref(
        cls,
        ref,

    ) -> NamespacePath:
        return cls(':'.join(
            (ref.__module__,
             getattr(ref, '__name__', ''))
        ))
