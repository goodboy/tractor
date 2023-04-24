import trio
from tractor.trionics import ipython_embed


async def main():
    doggy = 99
    kitty = 'meow'

    await ipython_embed()

    assert doggy
    assert kitty


if __name__ == '__main__':
    trio.run(main)
