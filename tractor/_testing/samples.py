import os
import random


def generate_single_byte_msgs(amount: int) -> bytes:
    return b''.join(str(i % 10).encode() for i in range(amount))


def generate_sample_messages(
    amount: int,
    rand_min: int = 0,
    rand_max: int = 0,
    silent: bool = False
) -> tuple[list[bytes], int]:

    msgs = []
    size = 0

    if not silent:
        print(f'\ngenerating {amount} messages...')

    for i in range(amount):
        msg = f'[{i:08}]'.encode('utf-8')

        if rand_max > 0:
            msg += os.urandom(
                random.randint(rand_min, rand_max))

        size += len(msg)

        msgs.append(msg)

        if not silent and i and i % 10_000 == 0:
            print(f'{i} generated')

    if not silent:
        print(f'done, {size:,} bytes in total')

    return msgs, size
