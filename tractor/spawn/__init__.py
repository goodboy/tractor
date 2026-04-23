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
Actor process spawning machinery using multiple backends.

- `._spawn`: cross-backend subactor-as-sub[proc|int] spawning
  and supervision routines.

Per-backend submodules (each exposes a single `*_proc()`
coroutine registered in `_spawn._methods`):

- `._trio`: the `trio`-native subprocess backend (default,
  all platforms), spawns via `trio.lowlevel.open_process()`.

- `._mp`: the stdlib `multiprocessing` backend variants — driven by
  the `mp.context` bound to `_spawn._ctx`:
  * `'mp_spawn'`,
  * `'mp_forkserver'`

Entry-point helpers live in `._entry`/`._mp_fixup_main`/
`._forkserver_override`.

NOTE: to avoid circular imports, this ``__init__`` does NOT eagerly
import submodules.

'''
