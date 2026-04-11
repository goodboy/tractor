import os


async def child_fn() -> str:
    return f"child OK  pid={os.getpid()}"
