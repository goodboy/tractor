"""
Per process state
"""
from typing import Optional
from collections import Mapping

import trio

_current_actor: Optional['Actor'] = None  # type: ignore


def current_actor() -> 'Actor':  # type: ignore
    """Get the process-local actor instance.
    """
    if _current_actor is None:
        raise RuntimeError("No local actor has been initialized yet")
    return _current_actor


class ActorContextInfo(Mapping):
    "Dyanmic lookup for local actor and task names"
    _context_keys = ('task', 'actor')

    def __len__(self):
        return len(self._context_keys)

    def __iter__(self):
        return iter(self._context_keys)

    def __getitem__(self, key: str):
        try:
            return {
                'task': trio.hazmat.current_task,
                'actor': current_actor
            }[key]().name
        except RuntimeError:
            # no local actor/task context initialized yet
            return f'no {key} context'
