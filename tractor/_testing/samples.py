import os
import random
import hashlib


def generate_single_byte_msgs(amount: int) -> bytes:
    '''
    Generate a byte instance of len `amount` with:

    ```
    byte_at_index(i) = (i % 10).encode()
    ```

    this results in constantly repeating sequences of:

    b'0123456789'

    '''
    return b''.join(str(i % 10).encode() for i in range(amount))


def generate_sample_messages(
    amount: int,
    rand_min: int = 0,
    rand_max: int = 0,
    silent: bool = False,
) -> tuple[str, list[bytes], int]:
    '''
    Generate bytes msgs for tests.

    Messages will have the following format:

    ```
    b'[{i:08}]' + os.urandom(random.randint(rand_min, rand_max))
    ```

    so for message index 25:

    b'[00000025]' + random_bytes

    '''
    msgs = []
    size = 0

    log_interval = None
    if not silent:
        print(f'\ngenerating {amount} messages...')

        # calculate an apropiate log interval based on
        # max message size
        max_msg_size = 10 + rand_max

        if max_msg_size <= 32 * 1024:
            log_interval = 10_000

        else:
            log_interval = 1000

    payload_hash = hashlib.sha256()
    for i in range(amount):
        msg = f'[{i:08}]'.encode('utf-8')

        if rand_max > 0:
            msg += os.urandom(
                random.randint(rand_min, rand_max))

        size += len(msg)

        payload_hash.update(msg)
        msgs.append(msg)

        if (
            not silent
            and
            i > 0
            and
            i % log_interval == 0
        ):
            print(f'{i} generated')

    if not silent:
        print(f'done, {size:,} bytes in total')

    return payload_hash.hexdigest(), msgs, size
