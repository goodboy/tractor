import json
from pathlib import Path
import sys

import trio
import tractor

from spawn_test_support.parent_main_inheritance_support import get_main_mod_name


async def main(api: str, output_path: str) -> None:
    async with tractor.open_nursery(start_method='trio') as an:
        if api == 'run_in_actor':
            replaying = await an.run_in_actor(
                get_main_mod_name,
                name='replaying-parent-main',
            )
            isolated = await an.run_in_actor(
                get_main_mod_name,
                name='isolated-parent-main',
                inherit_parent_main=False,
            )
            replaying_name = await replaying.result()
            isolated_name = await isolated.result()
        elif api == 'start_actor':
            replaying = await an.start_actor(
                'replaying-parent-main',
                enable_modules=[
                    'spawn_test_support.parent_main_inheritance_support',
                ],
            )
            isolated = await an.start_actor(
                'isolated-parent-main',
                enable_modules=[
                    'spawn_test_support.parent_main_inheritance_support',
                ],
                inherit_parent_main=False,
            )
            try:
                replaying_name = await replaying.run_from_ns(
                    'spawn_test_support.parent_main_inheritance_support',
                    'get_main_mod_name',
                )
                isolated_name = await isolated.run_from_ns(
                    'spawn_test_support.parent_main_inheritance_support',
                    'get_main_mod_name',
                )
            finally:
                await replaying.cancel_actor()
                await isolated.cancel_actor()
        else:
            raise ValueError(f'Unknown api: {api}')

    Path(output_path).write_text(
        json.dumps({
            'replaying': replaying_name,
            'isolated': isolated_name,
        })
    )


if __name__ == '__main__':
    trio.run(main, sys.argv[1], sys.argv[2])
