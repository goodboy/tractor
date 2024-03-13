'''
Reproduce a bug where enabling debug mode for a sub-actor actually causes
a hang on teardown...

'''
import asyncio

import trio
import tractor
