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
Utils to tame mp non-SC madeness

'''
import platform


def disable_mantracker():
    '''
    Disable all `multiprocessing` "resource tracking" machinery since
    it's an absolute multi-threaded mess of non-SC madness.

    '''
    from multiprocessing.shared_memory import SharedMemory


    # 3.13+ only.. can pass `track=False` to disable
    # all the resource tracker bs.
    # https://docs.python.org/3/library/multiprocessing.shared_memory.html
    if (_py_313 := (
            platform.python_version_tuple()[:-1]
            >=
            ('3', '13')
        )
    ):
        from functools import partial
        return partial(
            SharedMemory,
            track=False,
        )

    # !TODO, once we drop 3.12- we can obvi remove all this!
    else:
        from multiprocessing import (
            resource_tracker as mantracker,
        )

        # Tell the "resource tracker" thing to fuck off.
        class ManTracker(mantracker.ResourceTracker):
            def register(self, name, rtype):
                pass

            def unregister(self, name, rtype):
                pass

            def ensure_running(self):
                pass

        # "know your land and know your prey"
        # https://www.dailymotion.com/video/x6ozzco
        mantracker._resource_tracker = ManTracker()
        mantracker.register = mantracker._resource_tracker.register
        mantracker.ensure_running = mantracker._resource_tracker.ensure_running
        mantracker.unregister = mantracker._resource_tracker.unregister
        mantracker.getfd = mantracker._resource_tracker.getfd

        # use std type verbatim
        shmT = SharedMemory

    return shmT
