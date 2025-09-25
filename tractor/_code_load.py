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
(Hot) coad (re-)load utils for python.

'''
import importlib
from pathlib import Path
import sys
from types import ModuleType

# ?TODO, move this into internal libs?
# -[ ] we already use it in `modden.config._pymod` as well
def load_module_from_path(
    path: Path,
    module_name: str|None = None,
) -> ModuleType:
    '''
    Taken from SO,
    https://stackoverflow.com/a/67208147

    which is based on stdlib docs,
    https://docs.python.org/3/library/importlib.html#importing-a-source-file-directly

    '''
    module_name = module_name or path.stem
    spec = importlib.util.spec_from_file_location(
        module_name,
        str(path),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
