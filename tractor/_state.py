"""
Per process state
"""
from typing import Optional


_current_actor: Optional['Actor'] = None  # type: ignore


def current_actor() -> 'Actor':  # type: ignore
    """Get the process-local actor instance.
    """
    if not _current_actor:
        raise RuntimeError("No actor instance has been defined yet?")
    return _current_actor
