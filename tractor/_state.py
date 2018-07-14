"""
Per process state
"""
_current_actor = None


def current_actor() -> 'Actor':
    """Get the process-local actor instance.
    """
    return _current_actor
