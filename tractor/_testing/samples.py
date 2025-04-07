import hashlib
import numpy as np


def generate_single_byte_msgs(amount: int) -> bytes:
    '''
    Generate a byte instance of length `amount` with repeating ASCII digits 0..9.

    '''
    # array [0, 1, 2, ..., amount-1], take mod 10 => [0..9], and map 0->'0'(48)
    # up to 9->'9'(57).
    arr = np.arange(amount, dtype=np.uint8) % 10
    # move into ascii space
    arr += 48
    return arr.tobytes()


class RandomBytesGenerator:
    '''
    Generate bytes msgs for tests.

    messages will have the following format:

        b'[{i:08}]' + random_bytes

    so for message index 25:

        b'[00000025]' + random_bytes

    also generates sha256 hash of msgs.

    '''

    def __init__(
        self,
        amount: int,
        rand_min: int = 0,
        rand_max: int = 0
    ):
        if rand_max < rand_min:
            raise ValueError('rand_max must be >= rand_min')

        self._amount = amount
        self._rand_min = rand_min
        self._rand_max = rand_max
        self._index = 0
        self._hasher = hashlib.sha256()
        self._total_bytes = 0

        self._lengths = np.random.randint(
            rand_min,
            rand_max + 1,
            size=amount,
            dtype=np.int32
        )

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        if self._index == self._amount:
            raise StopIteration

        header = f'[{self._index:08}]'.encode('utf-8')

        length = int(self._lengths[self._index])
        msg = header + np.random.bytes(length)

        self._hasher.update(msg)
        self._total_bytes += length
        self._index += 1

        return msg

    @property
    def hexdigest(self) -> str:
        return self._hasher.hexdigest()

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def total_msgs(self) -> int:
        return self._amount

    @property
    def msgs_generated(self) -> int:
        return self._index

    @property
    def recommended_log_interval(self) -> int:
        max_msg_size = 10 + self._rand_max

        if max_msg_size <= 32 * 1024:
            return 10_000

        else:
            return 1000
