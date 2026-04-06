import sys


async def get_main_mod_name() -> str:
    return sys.modules['__main__'].__name__
