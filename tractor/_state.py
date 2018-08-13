"""
Per process state
"""
_current_actor = None


def current_actor() -> 'Actor':
    """Get the process-local actor instance.
    """
    if not _current_actor:
        raise RuntimeError("No actor instance has been defined yet?")
    return _current_actor
