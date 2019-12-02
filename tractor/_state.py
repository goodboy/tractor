"""
Per process state
"""
from typing import Optional

import trio

_current_actor: Optional['Actor'] = None  # type: ignore


def current_actor() -> 'Actor':  # type: ignore
    """Get the process-local actor instance.
    """
    if _current_actor is None:
        raise RuntimeError("No local actor has been initialized yet")
    return _current_actor


class ActorContextInfo:
    "Dyanmic lookup for local actor and task names"
    def __iter__(self):
        return iter(('task', 'actor'))

    def __getitem__(self, key: str):
        if key == 'task':
            try:
                return trio._core.current_task().name
            except RuntimeError:
                # not inside `trio.run()` yet
                return 'no task context'
        elif key == 'actor':
            try:
                return current_actor().name
            except RuntimeError:
                # no local actor initialize yet
                return 'no actor context'
