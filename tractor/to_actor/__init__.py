# tractor: distributed structured concurrency.
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
`tractor.to_actor`: high-level "one-shot" remote-task APIs.

Adopts the "run it over there" parlance from analogous
(sibling-library) APIs like `trio.to_thread` and
`anyio.to_process` but for SC-supervised actors: spawn (or
reuse) a subactor, schedule a single remote task, wait on
its result and (when the call owns the subactor) reap it.

The "spiritual successor" to (and eventual replacement of)
the `ActorNursery.run_in_actor()` API; see
https://github.com/goodboy/tractor/issues/477

'''
from ._api import (
    run as run,
)
